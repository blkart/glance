#    Copyright 2012 OpenStack Foundation
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.


GLANCE_VENDOR = "OpenStack Foundation"
GLANCE_PRODUCT = "OpenStack Glance"
GLANCE_PACKAGE = None  # OS distro package version suffix

loaded = False


class VersionInfo(object):
    release = "REDHATGLANCERELEASE"
    version = "REDHATGLANCEVERSION"

    def version_string(self):
        return self.version

    def cached_version_string(self):
        return self.version

    def release_string(self):
        return self.release

    def canonical_version_string(self):
        return self.version

    def version_string_with_vcs(self):
        return self.release


version_info = VersionInfo()
