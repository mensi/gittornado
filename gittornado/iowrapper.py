# -*- coding: utf-8 -*-
#
# Copyright 2011 Manuel Stocker <mensi@mensi.ch>
#
# This file is part of GitTornado.
#
# GitTornado is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# GitTornado is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with GitTornado.  If not, see http://www.gnu.org/licenses

import subprocess
import zlib
import os

import tornado.ioloop

from gittornado.util import get_date_header

import logging
logger = logging.getLogger(__name__)

class ProcessWrapper(object):
    """Wraps a subprocess and communicates with HTTP client
    
    Supports gzip compression and chunked transfer encoding
    """

    reading_chunks = False
    got_chunk = False
    headers_sent = False
    got_request = False
    sent_chunks = False

    gzip_decompressor = None
    gzip_header_seen = False

    process_input_buffer = ''

    output_prelude = ''

    def __init__(self, request, command, headers, output_prelude=''):
        """Wrap a subprocess
        
        :param request: tornado request object
        :param command: command to be given to subprocess.Popen 
        :param headers: headers to be included on success
        :param output_prelude: data to send before the output of the process
        """
        self.request = request
        self.headers = headers
        self.output_prelude = output_prelude

        # invoke process
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.PIPE, stdout=subprocess.PIPE)

        # check return status
        if self.process.poll() is not None:
            raise tornado.web.HTTPError(500, 'subprocess returned prematurely')

        # get fds
        self.fd_stdout = self.process.stdout.fileno()
        self.fd_stderr = self.process.stderr.fileno()
        self.fd_stdin = self.process.stdin.fileno()

        # register with ioloop
        self.ioloop = tornado.ioloop.IOLoop.instance()
        self.ioloop.add_handler(self.fd_stdout, self._handle_stdout_event, self.ioloop.READ | self.ioloop.ERROR)
        self.ioloop.add_handler(self.fd_stderr, self._handle_stderr_event, self.ioloop.READ | self.ioloop.ERROR)
        self.ioloop.add_handler(self.fd_stdin, self._handle_stdin_event, self.ioloop.WRITE | self.ioloop.ERROR)

        # is it gzipped? If yes, we initialize a zlib decompressobj
        if 'gzip' in request.headers.get('Content-Encoding', '').lower(): # HTTP/1.1 RFC says value is case-insensitive
            logger.debug("Gzipped request. Initializing decompressor.")
            self.gzip_decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS) # skip the gzip header

        if self.request.method == 'POST':
            # Handle chunked encoding
            if request.headers.get('Expect', None) == '100-continue' and request.headers.get('Transfer-Encoding', None) == 'chunked':
                logger.debug('Request uses chunked transfer encoding. Sending 100 Continue.')
                self.httpstream = self.request.connection.stream
                self.request.write("HTTP/1.1 100 (Continue)\r\n\r\n")
                self.read_chunks()
            else:
                logger.debug('Got complete request')
                if self.gzip_decompressor:
                    assert request.body[:2] == '\x1f\x8b', "gzip header"
                    self.process_input_buffer = self.gzip_decompressor.decompress(request.body)
                else:
                    self.process_input_buffer = request.body
                self.got_request = True
        else:
            logger.debug("Method %s has no input", self.request.method)
            self.got_request = True

    def read_chunks(self):
        """Read chunks from the HTTP client"""

        if self.reading_chunks and self.got_chunk:
            # we got on the fast-path and directly read from the buffer.
            # if we continue to recurse, this is going to blow up the stack.
            # so instead return
            #
            # NOTE: This actually is unnecessary as long as tornado guarantees that
            #       ioloop.add_callback always gets dispatched via the main io loop
            #       and they don't introduce a fast-path similar to read_XY
            logger.debug("Fast-Path detected, returning...")
            return

        while not self.got_request:
            self.reading_chunks = True
            self.got_chunk = False
            # chunk starts with length, so read it. This will then subsequently also read the chunk
            self.httpstream.read_until("\r\n", self._chunk_length)
            self.reading_chunks = False

            if self.got_chunk:
                # the previous read hit the fast path and read from the buffer
                # instead of going through the main polling loop. This means we 
                # should iteratively issue the next request
                logger.debug("Fast-Path detected, iterating...")
                continue
            else:
                break

        # if we arrive here, we read the complete request or
        # the ioloop has scheduled another call to read_chunks
        return

    def _chunk_length(self, data):
        """Received the chunk length"""

        assert data[-2:] == "\r\n", "CRLF"

        length = data[:-2].split(';')[0] # cut off optional length paramters
        length = int(length.strip(), 16) # length is in hex

        if length:
            logger.debug('Got chunk length: %d', length)
            self.httpstream.read_bytes(length + 2, self._chunk_data)
        else:
            logger.debug('Got last chunk (size 0)')
            self.got_request = True
            # enable input write event so the handler can finish things up 
            # when it has written all pending data
            self.ioloop.update_handler(self.fd_stdin, self.ioloop.WRITE | self.ioloop.ERROR)

    def _chunk_data(self, data):
        """Received chunk data"""

        assert data[-2:] == "\r\n", "CRLF"

        if self.gzip_decompressor:
            if not self.gzip_header_seen:
                assert data[:2] == '\x1f\x8b', "gzip header"
                self.gzip_header_seen = True

            self.process_input_buffer += self.gzip_decompressor.decompress(data[:-2])
        else:
            self.process_input_buffer += data[:-2]

        self.got_chunk = True

        if self.process_input_buffer:
            # since we now have data in the buffer, enable write events again
            logger.debug('Got data in buffer, interested in writing to process again')
            self.ioloop.update_handler(self.fd_stdin, self.ioloop.WRITE | self.ioloop.ERROR)

        # do NOT call read_chunks directly. This is to give git a chance to consume input.
        # we don't want to grow the buffer unnecessarily.
        # Additionally, this should mitigate the stack explosion mentioned in read_chunks
        self.ioloop.add_callback(self.read_chunks)

    def _handle_stdin_event(self, fd, events):
        """Eventhandler for stdin"""

        assert fd == self.fd_stdin

        if events & self.ioloop.ERROR:
            # An error at the end is expected since tornado maps HUP to ERROR
            logger.debug('Error on stdin')
            # ensure pipe is closed
            if not self.process.stdin.closed:
                self.process.stdin.close()
            # remove handler
            self.ioloop.remove_handler(self.fd_stdin)
            # if all fds are closed, we can finish
            return self._graceful_finish()

        # got data ready
        logger.debug('stdin ready for write')
        if self.process_input_buffer:
            count = os.write(fd, self.process_input_buffer)
            logger.debug('Wrote first %d bytes of %d total', count, len(self.process_input_buffer))
            self.process_input_buffer = self.process_input_buffer[count:]

        if not self.process_input_buffer:
            # consumed everything in the buffer
            if self.got_request:
                # we got the request and wrote everything to the process
                # this means we can close stdin and stop handling events
                # for it
                logger.debug('Got complete request, closing stdin')
                self.process.stdin.close()
                self.ioloop.remove_handler(fd)
            else:
                # There is more data bound to come from the client
                # so just disable write events for the moment until 
                # we got more to write
                logger.debug('Not interested in write events on stdin anymore')
                self.ioloop.update_handler(fd, self.ioloop.ERROR)

    def _handle_stdout_event(self, fd, events):
        """Eventhandler for stdout"""

        assert fd == self.fd_stdout

        if events & self.ioloop.READ:
            # got data ready to read
            data = ''

            # Now basically we have two cases: either the client supports
            # HTTP/1.1 in which case we can stream the answer in chunked mode
            # in HTTP/1.0 we need to send a content-length and thus buffer the complete output
            if self.request.supports_http_1_1():
                if not self.headers_sent:
                    self.sent_chunks = True
                    self.headers.update({'Date': get_date_header(), 'Transfer-Encoding': 'chunked'})
                    data = 'HTTP/1.1 200 OK\r\n' + '\r\n'.join([ k + ': ' + v for k, v in self.headers.items()]) + '\r\n\r\n'

                    if self.output_prelude:
                        data += hex(len(self.output_prelude))[2:] + "\r\n" # cut off 0x
                        data += self.output_prelude + "\r\n"

                    self.headers_sent = True

                payload = os.read(fd, 8192)
                if events & self.ioloop.ERROR: # there might be data remaining in the buffer if we got HUP, get it all
                    remainder = True
                    while remainder != '': # until EOF
                        remainder = os.read(fd, 8192)
                        payload += remainder

                data += hex(len(payload))[2:] + "\r\n" # cut off 0x
                data += payload + "\r\n"

            else:
                if not self.headers_sent:
                    # Use the over-eager blocking read that will get everything until we hit EOF
                    # this might actually be somewhat dangerous as noted in the subprocess documentation
                    # and lead to a deadlock. This is only a legacy mode for HTTP/1.0 clients anyway,
                    # so we might want to remove it entirely anyways
                    payload = self.process.stdout.read()
                    self.headers.update({'Date': get_date_header(), 'Content-Length': str(len(payload))})
                    data = 'HTTP/1.0 200 OK\r\n' + '\r\n'.join([ k + ': ' + v for k, v in self.headers.items()]) + '\r\n\r\n'
                    self.headers_sent = True
                    data += self.output_prelude + payload
                else:
                    # this is actually somewhat illegal as it messes with content-length but 
                    # it shouldn't happen anyways, as the read above should have read anything
                    # python docs say this can happen on ttys...
                    logger.error("This should not happen")
                    data = self.process.stdout.read()

            logger.debug('Sending stdout to client %d bytes: %r', len(data), data[:20])
            self.request.write(data)

        # now we can also have an error. This is because tornado maps HUP onto error
        # therefore, no elif here!
        if events & self.ioloop.ERROR:
            logger.debug('Error on stdout')
            # ensure file is closed
            if not self.process.stdout.closed:
                self.process.stdout.close()
            # remove handler
            self.ioloop.remove_handler(self.fd_stdout)
            # if all fds are closed, we can finish
            return self._graceful_finish()

    def _handle_stderr_event(self, fd, events):
        """Eventhandler for stderr"""

        assert fd == self.fd_stderr

        if events & self.ioloop.READ:
            # got data ready
            if not self.headers_sent:
                payload = self.process.stderr.read()

                data = 'HTTP/1.1 500 Internal Server Error\r\nDate: %s\r\nContent-Length: %d\r\n\r\n' % (get_date_header(), len(payload))
                self.headers_sent = True
                data += payload
            else:
                # see stdout
                logger.error("This should not happen (stderr)")
                data = self.process.stderr.read()

            logger.debug('Sending stderr to client: %r', data)
            self.request.write(data)

        if events & self.ioloop.ERROR:
            logger.debug('Error on stderr')
            # ensure file is closed
            if not self.process.stderr.closed:
                self.process.stderr.close()
            # remove handler
            self.ioloop.remove_handler(self.fd_stderr)
            # if all fds are closed, we can finish
            return self._graceful_finish()

    def _graceful_finish(self):
        """Detect if process has closed pipes and we can finish"""

        if not self.process.stdout.closed or not self.process.stderr.closed:
            return # stdout/stderr still open

        if not self.process.stdin.closed:
            self.process.stdin.close()

        logger.debug("Finishing up")

        if not self.headers_sent:
            logger.error("Empty response")
            # we didn't write any data, so this is probably an error
            payload = "did not produce any data"
            data = 'HTTP/1.1 500 Internal Server Error\r\nDate: %s\r\nContent-Length: %d\r\n\r\n' % (self._get_date(), len(payload))
            self.headers_sent = True
            data += payload
            self.request.write(data)

        # if we are in chunked mode, send end chunk with length 0
        elif self.sent_chunks:
            logger.debug("End chunk")
            self.request.write("0\r\n")
            #we could now send some more headers resp. trailers
            self.request.write("\r\n")

        self.request.finish()
