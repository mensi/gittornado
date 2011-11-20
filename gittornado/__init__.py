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

import urlparse

import tornado.web

from gittornado.iowrapper import ProcessWrapper

import logging
logger = logging.getLogger(__name__)

class BaseHandler(tornado.web.RequestHandler):
    auth = None
    auth_failed = None
    gitlookup = None
    gitcommand = None

    public_readble = True
    public_writable = False

    def initialize(self, **kwargs):
        for name, value in kwargs.items():
            if hasattr(self, name) and getattr(self, name) is None:
                setattr(self, name, value)

        # set defaults
        if self.gitcommand is None:
            self.gitcommand = 'git'

    def get_gitdir(self):
        """Determine the git repository for this request"""
        if self.gitlookup is None:
            raise tornado.web.HTTPError(500, 'no git lookup configured')

        gitdir = self.gitlookup(self.request)
        if gitdir is None:
            raise tornado.web.HTTPError(404, 'unable to find repository')
        logger.debug("Accessing git at: %s", gitdir)

        return gitdir

    def check_auth(self):
        """Check authentication/authorization of client"""
        # access permissions
        if self.auth is not None:
            return self.auth(self.request)

        return self.public_readble, self.public_writable

    def enforce_perms(self, rpc):
        read, write = self.check_auth()

        if rpc in ['git-receive-pack', 'receive-pack']:
            if not write:
                if self.auth_failed:
                    self.auth_failed(self.request)
                    self.request.finish()
                    return False
                else:
                    raise tornado.web.HTTPError(403, 'You are not allowed to perform this action')

        elif rpc in ['git-upload-pack', 'upload-pack']:
            if not read:
                if self.auth_failed:
                    self.auth_failed(self.request)
                    self.request.finish()
                    return False
                else:
                    raise tornado.web.HTTPError(403, 'You are not allowed to perform this action')

        else:
            raise tornado.web.HTTPError(400, 'Unknown RPC command')

        return True

class RPCHandler(BaseHandler):
    """Request handler for RPC calls
    
    Use this handler to handle example.git/git-upload-pack and example.git/git-receive-pack URLs"""
    @tornado.web.asynchronous
    def post(self):
        gitdir = self.get_gitdir()

        # get RPC command
        pathlets = self.request.path.strip('/').split('/')
        rpc = pathlets[-1]
        if not self.enforce_perms(rpc):
            return
        rpc = rpc[4:]

        ProcessWrapper(self.request, [self.gitcommand, rpc, '--stateless-rpc', gitdir],
                       {'Content-Type': 'application/x-git-%s-result' % rpc})

class InfoRefsHandler(BaseHandler):
    """Request handler for info/refs
    
    Use this handler to handle example.git/info/refs?service= URLs"""
    @tornado.web.asynchronous
    def get(self):
        gitdir = self.get_gitdir()

        rpc = urlparse.parse_qs(self.request.query).get('service', [''])[0]

        if not rpc:
            rpc = 'git-upload-pack'
            #raise tornado.web.HTTPError(400, 'Only smart HTTP mode supported')

        if not self.enforce_perms(rpc):
            return

        rpc = rpc[4:]

        prelude = '# service=git-' + rpc
        prelude = str(hex(len(prelude) + 4)[2:].rjust(4, '0')) + prelude
        prelude += '0000' # packet flush               

        ProcessWrapper(self.request, [self.gitcommand, rpc, '--stateless-rpc', '--advertise-refs', gitdir],
                       {'Content-Type': 'application/x-git-%s-advertisement' % rpc,
                        'Expires': 'Fri, 01 Jan 1980 00:00:00 GMT',
                        'Pragma': 'no-cache',
                        'Cache-Control': 'no-cache, max-age=0, must-revalidate'}, prelude)
