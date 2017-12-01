#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os

from nodepool import config as nodepool_config
from nodepool import tests
from nodepool.driver import Drivers


class TestDrivers(tests.DBTestCase):
    def setup_config(self, filename):
        drivers_dir = os.path.join(
            os.path.dirname(__file__), 'fixtures', 'drivers')
        Drivers.load([drivers_dir])
        return super().setup_config(filename)

    def test_external_driver_config(self):
        configfile = self.setup_config('external_driver.yaml')
        nodepool_config.loadConfig(configfile)
        self.assertIn("config", Drivers.get("test"))

    def test_external_driver_handler(self):
        configfile = self.setup_config('external_driver.yaml')
        pool = self.useNodepool(configfile, watermark_sleep=1)
        pool.start()
        nodes = self.waitForNodes('test-label')
        self.assertEqual(len(nodes), 1)
