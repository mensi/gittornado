#!/usr/bin/env python
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

from setuptools import setup, find_packages

setup(name='GitTornado',
      install_requires='tornado',
      description='Tornado-based implementation of the git HTTP protocol supporting gzip and chunked transfers',
      keywords='git http',
      version='0.1',
      url='',
      license='GPL',
      author='Manuel Stocker',
      author_email='mensi@mensi.ch',
      long_description="""GitTornado is an implementation of the git HTTP-based protocol.""",
      packages=find_packages(),
      zip_safe=True,
      entry_points={'console_scripts': ['gittornado = gittornado.server:main']})
