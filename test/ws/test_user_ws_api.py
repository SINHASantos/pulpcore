#!/usr/bin/python
#
# Copyright (c) 2010 Red Hat, Inc.
#
# This software is licensed to you under the GNU General Public License,
# version 2 (GPLv2). There is NO WARRANTY for this software, express or
# implied, including the implied warranties of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. You should have received a copy of GPLv2
# along with this software; if not, see
# http://www.gnu.org/licenses/old-licenses/gpl-2.0.txt.
#
# Red Hat trademarks are not licensed under GPLv2. No permission is
# granted to use or replicate Red Hat trademarks that are incorporated
# in this software or its documentation.

import sys
import os

# Pulp
srcdir = os.path.abspath(os.path.dirname(__file__)) + "/../../src/"
sys.path.insert(0, srcdir)

commondir = os.path.abspath(os.path.dirname(__file__)) + '/../unit/'
sys.path.insert(0, commondir)

from pulptools.connection import UserConnection

from test_users import TestUsers

import testutil

class RemoteTestUsers(TestUsers):
    """
    This class subclasses TestApi and overrides the API handlers to actually
    use the same classes the CLI uses.  This ensures we are using the API exactly
    like we are when we call the pulp python API directly.
    
    The overridden testcases in this class indicate tests that *dont* pass yet.
    """

    def setUp(self):
        d = dict(host='localhost', port=8811, username="admin", password="admin")
        self.config = testutil.load_test_config()
        self.uapi = UserConnection(**d)
        
