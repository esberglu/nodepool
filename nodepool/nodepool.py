#!/usr/bin/env python

# Copyright (C) 2011-2014 OpenStack Foundation
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
#
# See the License for the specific language governing permissions and
# limitations under the License.

import apscheduler.schedulers.background
import apscheduler.triggers.cron
import json
import logging
import os
import os.path
import paramiko
import pprint
import random
import socket
import threading
import time

import allocation
import jenkins_manager
import nodedb
import exceptions
import nodeutils as utils
import provider_manager
import stats
import config as nodepool_config

import zk

MINS = 60
HOURS = 60 * MINS

WATERMARK_SLEEP = 10         # Interval between checking if new servers needed
IMAGE_TIMEOUT = 6 * HOURS    # How long to wait for an image save
CONNECT_TIMEOUT = 10 * MINS  # How long to try to connect after a server
                             # is ACTIVE
NODE_CLEANUP = 8 * HOURS     # When to start deleting a node that is not
                             # READY or HOLD
TEST_CLEANUP = 5 * MINS      # When to start deleting a node that is in TEST
IMAGE_CLEANUP = 8 * HOURS    # When to start deleting an image that is not
                             # READY or is not the current or previous image
DELETE_DELAY = 1 * MINS      # Delay before deleting a node that has completed
                             # its job.
SUSPEND_WAIT_TIME = 30       # How long to wait between checks for ZooKeeper
                             # connectivity if it disappears.


class LaunchNodepoolException(Exception):
    statsd_key = 'error.nodepool'


class LaunchStatusException(Exception):
    statsd_key = 'error.status'


class LaunchNetworkException(Exception):
    statsd_key = 'error.network'


class LaunchAuthException(Exception):
    statsd_key = 'error.auth'


class NodeCompleteThread(threading.Thread):
    log = logging.getLogger("nodepool.NodeCompleteThread")

    def __init__(self, nodepool, nodename, jobname, result, branch):
        threading.Thread.__init__(self, name='NodeCompleteThread for %s' %
                                  nodename)
        self.nodename = nodename
        self.nodepool = nodepool
        self.jobname = jobname
        self.result = result
        self.branch = branch
        self.statsd = stats.get_client()

    def run(self):
        try:
            with self.nodepool.getDB().getSession() as session:
                self.handleEvent(session)
        except Exception:
            self.log.exception("Exception handling event for %s:" %
                               self.nodename)

    def handleEvent(self, session):
        node = session.getNodeByNodename(self.nodename)
        if not node:
            self.log.debug("Unable to find node with nodename: %s" %
                           self.nodename)
            return

        if node.state == nodedb.HOLD:
            self.log.info("Node id: %s is complete but in HOLD state" %
                          node.id)
            return

        nodepool_job = session.getJobByName(self.jobname)
        if (nodepool_job and nodepool_job.hold_on_failure and
            self.result != 'SUCCESS'):
            held_nodes = session.getNodes(state=nodedb.HOLD)
            held_nodes = [n for n in held_nodes if self.jobname in n.comment]
            if len(held_nodes) >= nodepool_job.hold_on_failure:
                self.log.info("Node id: %s has failed %s but %s nodes "
                              "are already held for that job" % (
                                  node.id, self.jobname, len(held_nodes)))
            else:
                node.state = nodedb.HOLD
                node.comment = "Automatically held after failing %s" % (
                    self.jobname,)
                self.log.info("Node id: %s failed %s, automatically holding" % (
                    node.id, self.jobname))
                self.nodepool.updateStats(session, node.provider_name)
                return

        target = self.nodepool.config.targets[node.target_name]
        if self.jobname == target.jenkins_test_job:
            self.log.debug("Test job for node id: %s complete, result: %s" %
                           (node.id, self.result))
            if self.result == 'SUCCESS':
                jenkins = self.nodepool.getJenkinsManager(target)
                old = jenkins.relabelNode(node.nodename, [node.image_name])
                if not old:
                    old = '[unlabeled]'
                self.log.info("Relabeled jenkins node id: %s from %s to %s" %
                              (node.id, old, node.image_name))
                node.state = nodedb.READY
                self.log.info("Node id: %s is ready" % node.id)
                self.nodepool.updateStats(session, node.provider_name)
                return
            self.log.info("Node id: %s failed acceptance test, deleting" %
                          node.id)

        if self.statsd and self.result == 'SUCCESS':
            start = node.state_time
            dt = int((time.time() - start) * 1000)

            # nodepool.job.tempest
            key = 'nodepool.job.%s' % self.jobname
            self.statsd.timing(key + '.runtime', dt)
            self.statsd.incr(key + '.builds')

            # nodepool.job.tempest.master
            key += '.%s' % self.branch
            self.statsd.timing(key + '.runtime', dt)
            self.statsd.incr(key + '.builds')

            # nodepool.job.tempest.master.devstack-precise
            key += '.%s' % node.label_name
            self.statsd.timing(key + '.runtime', dt)
            self.statsd.incr(key + '.builds')

            # nodepool.job.tempest.master.devstack-precise.rax-ord
            key += '.%s' % node.provider_name
            self.statsd.timing(key + '.runtime', dt)
            self.statsd.incr(key + '.builds')

        time.sleep(DELETE_DELAY)
        self.nodepool.deleteNode(node.id)


class InstanceDeleter(threading.Thread):
    log = logging.getLogger("nodepool.InstanceDeleter")

    def __init__(self, nodepool, provider_name, external_id):
        threading.Thread.__init__(self, name='InstanceDeleter for %s %s' %
                                  (provider_name, external_id))
        self.nodepool = nodepool
        self.provider_name = provider_name
        self.external_id = external_id

    def run(self):
        try:
            self.nodepool._deleteInstance(self.provider_name,
                                          self.external_id)
        except Exception:
            self.log.exception("Exception deleting instance %s from %s:" %
                               (self.external_id, self.provider_name))


class NodeDeleter(threading.Thread):
    log = logging.getLogger("nodepool.NodeDeleter")

    def __init__(self, nodepool, node_id):
        threading.Thread.__init__(self, name='NodeDeleter for %s' % node_id)
        self.node_id = node_id
        self.nodepool = nodepool

    def run(self):
        try:
            with self.nodepool.getDB().getSession() as session:
                node = session.getNode(self.node_id)
                self.nodepool._deleteNode(session, node)
        except Exception:
            self.log.exception("Exception deleting node %s:" %
                               self.node_id)


class NodeLauncher(threading.Thread):
    log = logging.getLogger("nodepool.NodeLauncher")

    def __init__(self, nodepool, provider, label, target, node_id, timeout,
                 launch_timeout):
        threading.Thread.__init__(self, name='NodeLauncher for %s' % node_id)
        self.provider = provider
        self.label = label
        self.image = provider.images[label.image]
        self.target = target
        self.node_id = node_id
        self.timeout = timeout
        self.nodepool = nodepool
        self.launch_timeout = launch_timeout

    def run(self):
        try:
            self._run()
        except Exception:
            self.log.exception("Exception in run method:")

    def _run(self):
        with self.nodepool.getDB().getSession() as session:
            self.log.debug("Launching node id: %s" % self.node_id)
            try:
                self.node = session.getNode(self.node_id)
                self.manager = self.nodepool.getProviderManager(self.provider)
            except Exception:
                self.log.exception("Exception preparing to launch node id: %s:"
                                   % self.node_id)
                return

            try:
                start_time = time.time()
                dt = self.launchNode(session)
                failed = False
                statsd_key = 'ready'
                self.log.debug('Node %s ready in provider: %s' %
                               (self.node_id, self.provider.name))
            except exceptions.TimeoutException as e:
                # Don't log exception for timeouts. Each one has
                # a specific Exception, and we know it's a timeout, so
                # the traceback in the log is just noise
                self.log.error("Timeout launching node id: %s "
                                   "in provider: %s error: %s" %
                                   (self.node_id, self.provider.name,
                                    str(e)))
                dt = int((time.time() - start_time) * 1000)
                failed = True
                statsd_key = e.statsd_key
            except Exception as e:
                self.log.exception("%s launching node id: %s "
                                   "in provider: %s error:" %
                                   (e.__class__.__name__,
                                    self.node_id, self.provider.name))
                dt = int((time.time() - start_time) * 1000)
                failed = True
                if hasattr(e, 'statsd_key'):
                    statsd_key = e.statsd_key
                else:
                    statsd_key = 'error.unknown'

            try:

                self.nodepool.launchStats(statsd_key, dt, self.image.name,
                                          self.provider.name,
                                          self.target.name,
                                          self.node.az,
                                          self.node.manager_name)
            except Exception:
                self.log.exception("Exception reporting launch stats:")

            if failed:
                try:
                    self.nodepool.deleteNode(self.node_id)
                except Exception:
                    self.log.exception("Exception deleting node id: %s:" %
                                       self.node_id)

    def launchNode(self, session):
        start_time = time.time()
        timestamp = int(start_time)

        hostname = self.target.hostname.format(
            label=self.label, provider=self.provider, node_id=self.node.id,
            timestamp=str(timestamp))
        self.node.hostname = hostname
        self.node.nodename = hostname.split('.')[0]
        self.node.target_name = self.target.name

        cloud_image = self.nodepool.zk.getMostRecentImageUpload(
            self.image.name, self.provider.name)
        if not cloud_image:
            raise LaunchNodepoolException("Unable to find current cloud"
                                          "image %s in %s" %
                                          (self.image.name,
                                           self.provider.name))

        self.log.info("Creating server with hostname %s in %s from image %s "
                      "for node id: %s" % (hostname, self.provider.name,
                                           self.image.name, self.node_id))
        server = self.manager.createServer(
            hostname, self.image.min_ram, cloud_image.external_id,
            name_filter=self.image.name_filter, az=self.node.az,
            config_drive=self.image.config_drive,
            nodepool_node_id=self.node_id,
            nodepool_image_name=self.image.name)
        server_id = server['id']
        self.node.external_id = server_id
        session.commit()

        self.log.debug("Waiting for server %s for node id: %s" %
                       (server_id, self.node.id))
        server = self.manager.waitForServer(server, self.launch_timeout)
        if server['status'] != 'ACTIVE':
            raise LaunchStatusException("Server %s for node id: %s "
                                        "status: %s" %
                                        (server_id, self.node.id,
                                         server['status']))

        ip = server.get('public_v4')
        ip_v6 = server.get('public_v6')
        if self.provider.ipv6_preferred:
            if ip_v6:
                ip = ip_v6
            else:
                self.log.warning('Preferred ipv6 not available, '
                                 'falling back to ipv4.')
        if not ip:
            self.log.debug(
                "Server data for failed IP: %s" % pprint.pformat(
                    server))
            raise LaunchNetworkException("Unable to find public IP of server")

        self.node.ip_private = server.get('private_v4')
        # devstack-gate multi-node depends on private_v4 being populated
        # with something. On clouds that don't have a private address, use
        # the public.
        if not self.node.ip_private:
            self.node.ip_private = server.get('public_v4')
        self.node.ip = ip
        self.log.debug("Node id: %s is running, ipv4: %s, ipv6: %s" %
                       (self.node.id, server.get('public_v4'),
                        server.get('public_v6')))

        self.log.debug("Node id: %s testing ssh at ip: %s" %
                       (self.node.id, ip))
        connect_kwargs = dict(key_filename=self.image.private_key)
        if not utils.ssh_connect(ip, self.image.username,
                                 connect_kwargs=connect_kwargs,
                                 timeout=self.timeout):
            raise LaunchAuthException("Unable to connect via ssh")

        # Save the elapsed time for statsd
        dt = int((time.time() - start_time) * 1000)

        if self.label.subnodes:
            self.log.info("Node id: %s is waiting on subnodes" % self.node.id)

            while ((time.time() - start_time) < (NODE_CLEANUP - 60)):
                session.commit()
                ready_subnodes = [n for n in self.node.subnodes
                                  if n.state == nodedb.READY]
                if len(ready_subnodes) == self.label.subnodes:
                    break
                time.sleep(5)

        nodelist = []
        for subnode in self.node.subnodes:
            nodelist.append(('sub', subnode))
        nodelist.append(('primary', self.node))

        self.writeNodepoolInfo(nodelist)
        if self.label.ready_script:
            self.runReadyScript(nodelist)

        # Do this before adding to jenkins to avoid a race where
        # Jenkins might immediately use the node before we've updated
        # the state:
        if self.target.jenkins_test_job:
            self.node.state = nodedb.TEST
            self.log.info("Node id: %s is in testing" % self.node.id)
        else:
            self.node.state = nodedb.READY
            self.log.info("Node id: %s is ready" % self.node.id)
        self.nodepool.updateStats(session, self.provider.name)

        if self.target.jenkins_url:
            self.log.debug("Adding node id: %s to jenkins" % self.node.id)
            self.createJenkinsNode()
            self.log.info("Node id: %s added to jenkins" % self.node.id)

        return dt

    def createJenkinsNode(self):
        jenkins = self.nodepool.getJenkinsManager(self.target)

        args = dict(name=self.node.nodename,
                    host=self.node.ip,
                    description='Dynamic single use %s node' % self.label.name,
                    executors=1,
                    root=self.image.user_home)
        if not self.target.jenkins_test_job:
            args['labels'] = self.label.name
        if self.target.jenkins_credentials_id:
            args['credentials_id'] = self.target.jenkins_credentials_id
        else:
            args['username'] = self.image.username
            args['private_key'] = self.image.private_key

        jenkins.createNode(**args)

        if self.target.jenkins_test_job:
            params = dict(NODE=self.node.nodename)
            jenkins.startBuild(self.target.jenkins_test_job, params)

    def writeNodepoolInfo(self, nodelist):
        key = paramiko.RSAKey.generate(2048)
        public_key = key.get_name() + ' ' + key.get_base64()

        for role, n in nodelist:
            connect_kwargs = dict(key_filename=self.image.private_key)
            host = utils.ssh_connect(n.ip, self.image.username,
                                     connect_kwargs=connect_kwargs,
                                     timeout=self.timeout)
            if not host:
                raise Exception("Unable to log in via SSH")

            host.ssh("test for config dir", "ls /etc/nodepool")

            ftp = host.client.open_sftp()

            # The Role of this node
            f = ftp.open('/etc/nodepool/role', 'w')
            f.write(role + '\n')
            f.close()
            # The IP of this node
            f = ftp.open('/etc/nodepool/node', 'w')
            f.write(n.ip + '\n')
            f.close()
            # The private IP of this node
            f = ftp.open('/etc/nodepool/node_private', 'w')
            f.write(n.ip_private + '\n')
            f.close()
            # The IP of the primary node of this node set
            f = ftp.open('/etc/nodepool/primary_node', 'w')
            f.write(self.node.ip + '\n')
            f.close()
            # The private IP of the primary node of this node set
            f = ftp.open('/etc/nodepool/primary_node_private', 'w')
            f.write(self.node.ip_private + '\n')
            f.close()
            # The IPs of all sub nodes in this node set
            f = ftp.open('/etc/nodepool/sub_nodes', 'w')
            for subnode in self.node.subnodes:
                f.write(subnode.ip + '\n')
            f.close()
            # The private IPs of all sub nodes in this node set
            f = ftp.open('/etc/nodepool/sub_nodes_private', 'w')
            for subnode in self.node.subnodes:
                f.write(subnode.ip_private + '\n')
            f.close()
            # The SSH key for this node set
            f = ftp.open('/etc/nodepool/id_rsa', 'w')
            key.write_private_key(f)
            f.close()
            f = ftp.open('/etc/nodepool/id_rsa.pub', 'w')
            f.write(public_key + '\n')
            f.close()
            # Provider information for this node set
            f = ftp.open('/etc/nodepool/provider', 'w')
            f.write('NODEPOOL_PROVIDER=%s\n' % self.provider.name)
            f.write('NODEPOOL_CLOUD=%s\n' % self.provider.cloud_config.name)
            f.write('NODEPOOL_REGION=%s\n' % (
                self.provider.region_name or '',))
            f.write('NODEPOOL_AZ=%s\n' % (self.node.az or '',))
            f.close()
            # The instance UUID for this node
            f = ftp.open('/etc/nodepool/uuid', 'w')
            f.write(n.external_id + '\n')
            f.close()

            ftp.close()

    def runReadyScript(self, nodelist):
        for role, n in nodelist:
            connect_kwargs = dict(key_filename=self.image.private_key)
            host = utils.ssh_connect(n.ip, self.image.username,
                                     connect_kwargs=connect_kwargs,
                                     timeout=self.timeout)
            if not host:
                raise Exception("Unable to log in via SSH")

            env_vars = ''
            for k, v in os.environ.items():
                if k.startswith('NODEPOOL_'):
                    env_vars += ' %s="%s"' % (k, v)
            host.ssh("run ready script",
                     "cd /opt/nodepool-scripts && %s ./%s %s" %
                     (env_vars, self.label.ready_script, n.hostname),
                     output=True)


class SubNodeLauncher(threading.Thread):
    log = logging.getLogger("nodepool.SubNodeLauncher")

    def __init__(self, nodepool, provider, label, subnode_id,
                 node_id, node_target_name, timeout, launch_timeout, node_az,
                 manager_name):
        threading.Thread.__init__(self, name='SubNodeLauncher for %s'
                                  % subnode_id)
        self.provider = provider
        self.label = label
        self.image = provider.images[label.image]
        self.node_target_name = node_target_name
        self.subnode_id = subnode_id
        self.node_id = node_id
        self.timeout = timeout
        self.nodepool = nodepool
        self.launch_timeout = launch_timeout
        self.node_az = node_az
        self.manager_name = manager_name

    def run(self):
        try:
            self._run()
        except Exception:
            self.log.exception("Exception in run method:")

    def _run(self):
        with self.nodepool.getDB().getSession() as session:
            self.log.debug("Launching subnode id: %s for node id: %s" %
                           (self.subnode_id, self.node_id))
            try:
                self.subnode = session.getSubNode(self.subnode_id)
                self.manager = self.nodepool.getProviderManager(self.provider)
            except Exception:
                self.log.exception("Exception preparing to launch subnode "
                                   "id: %s for node id: %s:"
                                   % (self.subnode_id, self.node_id))
                return

            try:
                start_time = time.time()
                dt = self.launchSubNode(session)
                failed = False
                statsd_key = 'ready'
            except Exception as e:
                self.log.exception("%s launching subnode id: %s "
                                   "for node id: %s in provider: %s error:" %
                                   (e.__class__.__name__, self.subnode_id,
                                    self.node_id, self.provider.name))
                dt = int((time.time() - start_time) * 1000)
                failed = True
                if hasattr(e, 'statsd_key'):
                    statsd_key = e.statsd_key
                else:
                    statsd_key = 'error.unknown'

            try:
                self.nodepool.launchStats(statsd_key, dt, self.image.name,
                                          self.provider.name,
                                          self.node_target_name,
                                          self.node_az,
                                          self.manager_name)
            except Exception:
                self.log.exception("Exception reporting launch stats:")

            if failed:
                try:
                    self.nodepool.deleteSubNode(self.subnode, self.manager)
                except Exception:
                    self.log.exception("Exception deleting subnode id: %s: "
                                       "for node id: %s:" %
                                       (self.subnode_id, self.node_id))
                    return

    def launchSubNode(self, session):
        start_time = time.time()
        timestamp = int(start_time)

        target = self.nodepool.config.targets[self.node_target_name]
        hostname = target.subnode_hostname.format(
            label=self.label, provider=self.provider, node_id=self.node_id,
            subnode_id=self.subnode_id, timestamp=str(timestamp))
        self.subnode.hostname = hostname
        self.subnode.nodename = hostname.split('.')[0]

        cloud_image = self.nodepool.zk.getMostRecentImageUpload(
            self.image.name, self.provider.name)
        if not cloud_image:
            raise LaunchNodepoolException("Unable to find current cloud "
                                          "image %s in %s" %
                                          (self.image.name,
                                           self.provider.name))

        self.log.info("Creating server with hostname %s in %s from image %s "
                      "for subnode id: %s for node id: %s"
                      % (hostname, self.provider.name,
                         self.image.name, self.subnode_id, self.node_id))
        server = self.manager.createServer(
            hostname, self.image.min_ram, cloud_image.external_id,
            name_filter=self.image.name_filter, az=self.node_az,
            config_drive=self.image.config_drive,
            nodepool_node_id=self.node_id,
            nodepool_image_name=self.image.name)
        server_id = server['id']
        self.subnode.external_id = server_id
        session.commit()

        self.log.debug("Waiting for server %s for subnode id: %s for "
                       "node id: %s" %
                       (server_id, self.subnode_id, self.node_id))
        server = self.manager.waitForServer(server, self.launch_timeout)
        if server['status'] != 'ACTIVE':
            raise LaunchStatusException("Server %s for subnode id: "
                                        "%s for node id: %s "
                                        "status: %s" %
                                        (server_id, self.subnode_id,
                                         self.node_id, server['status']))

        ip = server.get('public_v4')
        ip_v6 = server.get('public_v6')
        if self.provider.ipv6_preferred:
            if ip_v6:
                ip = ip_v6
            else:
                self.log.warning('Preferred ipv6 not available, '
                                 'falling back to ipv4.')
        if not ip:
            raise LaunchNetworkException("Unable to find public IP of server")

        self.subnode.ip_private = server.get('private_v4')
        # devstack-gate multi-node depends on private_v4 being populated
        # with something. On clouds that don't have a private address, use
        # the public.
        if not self.subnode.ip_private:
            self.subnode.ip_private = server.get('public_v4')
        self.subnode.ip = ip
        self.log.debug("Subnode id: %s for node id: %s is running, "
                       "ipv4: %s, ipv6: %s" %
                       (self.subnode_id, self.node_id, server.get('public_v4'),
                        server.get('public_v6')))

        self.log.debug("Subnode id: %s for node id: %s testing ssh at ip: %s" %
                       (self.subnode_id, self.node_id, ip))
        connect_kwargs = dict(key_filename=self.image.private_key)
        if not utils.ssh_connect(ip, self.image.username,
                                 connect_kwargs=connect_kwargs,
                                 timeout=self.timeout):
            raise LaunchAuthException("Unable to connect via ssh")

        # Save the elapsed time for statsd
        dt = int((time.time() - start_time) * 1000)

        self.subnode.state = nodedb.READY
        self.log.info("Subnode id: %s for node id: %s is ready"
                      % (self.subnode_id, self.node_id))
        self.nodepool.updateStats(session, self.provider.name)

        return dt


class NodeRequestWorker(threading.Thread):
    '''
    Class to process a single node request.

    The ProviderWorker thread will instantiate a class of this type for each
    node request that it pulls from ZooKeeper. That request will be assigned
    to this thread for it to process.
    '''

    def __init__(self, zk, launcher_id, provider, request):
        '''
        :param ZooKeeper zk: Connected ZooKeeper object.
        :param str launcher_id: ID of the launcher handling the request.
        :param Provider provider: Provider object from the config file.
        :param NodeRequest request: The request to handle.
        '''
        threading.Thread.__init__(
            self, name='NodeRequestWorker.%s' % request.id
        )
        self.log = logging.getLogger("nodepool.%s" % self.name)
        self.zk = zk
        self.launcher_id = launcher_id
        self.provider = provider
        self.request = request

    def _imagesAvailable(self):
        '''
        Determines if the requested images are available for this provider.

        ZooKeeper is queried for an image uploaded to the provider that is
        in the READY state.

        :returns: True if it is available, False otherwise.
        '''
        for img in self.request.node_types:
            if not self.zk.getMostRecentImageUpload(img, self.provider.name):
                return False
        return True

    def _countNodes(self):
        '''
        Query ZooKeeper to determine the number of provider nodes launched.

        :returns: An integer for the number launched for this provider.
        '''
        count = 0
        for node_id in self.zk.getNodes():
            node = self.zk.getNode(node_id)
            if node and node.provider == self.provider.name:
                count += 1
        return count

    def _wouldExceedQuota(self):
        '''
        Determines if request would exceed provider quota.

        :returns: True if quota would be exceeded, False otherwise.
        '''
        provider_max = self.provider.max_servers
        num_requested = len(self.request.node_types)
        num_in_use = self._countNodes()
        return num_requested + num_in_use > provider_max

    def run(self):
        self.log.debug("Handling request %s" % self.request)
        try:
            self._run()
        except Exception:
            self.log.exception("Exception in NodeRequestWorker:")
            self.request.state = zk.FAILED
            self.zk.updateNodeRequest(self.request)
            self.zk.unlockNodeRequest(self.request)

    def _run(self):
        '''
        Main body for the NodeRequestWorker.

        note:: This code is a bit racey in its calculation of the number of
            nodes in use for quota purposes. It is possible for multiple
            launchers to be doing this calculation at the same time. Since we
            currently have no locking mechanism around the "in use"
            calculation, if we are at the edge of the quota, one of the
            launchers could attempt to launch a new node after the other
            launcher has already started doing so. This would cause an
            expected failure from the underlying library, which is ok for now.

        Algorithm from spec::

           # If image not available, decline
           # If request > quota, decline
           # If request < quota and request > available nodes (due to current
             usage), begin satisfying the request and do not process further
             requests until satisfied
           # If request < quota and request < available nodes, satisfy the
             request and continue processing further requests
        '''
        if not self._imagesAvailable() or self._wouldExceedQuota():
            self.request.declined_by.append(self.launcher_id)
            launchers = set(self.zk.getRegisteredLaunchers())
            if launchers.issubset(set(self.request.declined_by)):
                # All launchers have declined it
                self.request.state = zk.FAILED
            self.zk.updateNodeRequest(self.request)
            self.zk.unlockNodeRequest(self.request)
            return

        # TODO(Shrews): Determine node availability and if we need to launch
        # new nodes, or reuse existing nodes.

        self.request.state = zk.PENDING
        self.zk.updateNodeRequest(self.request)

        # TODO(Shrews): Make magic happen here

        self.request.state = zk.FULFILLED
        self.zk.updateNodeRequest(self.request)
        self.zk.unlockNodeRequest(self.request)


class ProviderWorker(threading.Thread):
    '''
    Class that manages node requests for a single provider.

    The NodePool thread will instantiate a class of this type for each
    provider found in the nodepool configuration file. If the provider to
    which this thread is assigned is removed from the configuration file, then
    that will be recognized and this thread will shut itself down.
    '''

    def __init__(self, configfile, zk, provider):
        threading.Thread.__init__(
            self, name='ProviderWorker.%s' % provider.name
        )
        self.log = logging.getLogger("nodepool.%s" % self.name)
        self.provider = provider
        self.zk = zk
        self.running = False
        self.configfile = configfile
        self.workers = []
        self.launcher_id = "%s-%s-%s" % (socket.gethostname(),
                                         os.getpid(),
                                         self.ident)

    #----------------------------------------------------------------
    # Private methods
    #----------------------------------------------------------------

    def _updateProvider(self):
        '''
        Update the provider definition from the config file.

        If this provider has been removed from the config, we need to
        stop processing the request queue. This will effectively cause
        this thread to terminate.
        '''
        config = nodepool_config.loadConfig(self.configfile)

        if self.provider.name not in config.providers.keys():
            self.log.info("Provider %s removed from config"
                          % self.provider.name)
            self.stop()

            # TODO(Shrews): Should we remove any existing nodes from the
            # provider here?
        else:
            self.provider = config.providers[self.provider.name]

    def _processRequests(self):
        self.log.debug("Getting node request from ZK queue")

        for req_id in self.zk.getNodeRequests():
            # Short-circuit for limited request handling
            if (self.provider.max_concurrency > 0
                and self._activeWorkers() >= self.provider.max_concurrency
            ):
                return

            req = self.zk.getNodeRequest(req_id)
            if not req:
                continue

            # Only interested in unhandled requests
            if req.state != zk.REQUESTED:
                continue

            # Skip it if we've already declined
            if self.launcher_id in req.declined_by:
                continue

            try:
                self.zk.lockNodeRequest(req, blocking=False)
            except exceptions.ZKLockException:
                continue

            # Make sure the state didn't change on us
            if req.state != zk.REQUESTED:
                self.zk.unlockNodeRequest(req)
                continue

            # Got a lock, so assign it
            self.log.info("Assigning node request %s" % req.id)
            t = NodeRequestWorker(self.zk, self.launcher_id,
                                  self.provider, req)
            t.start()
            self.workers.append(t)

    def _activeWorkers(self):
        '''
        Return a count of the number of requests actively being handled.

        This serves the dual-purpose of also removing completed requests from
        our list of tracked threads.
        '''
        active = []
        for w in self.workers:
            if w.isAlive():
                active.append(w)
        self.workers = active
        return len(self.workers)

    #----------------------------------------------------------------
    # Public methods
    #----------------------------------------------------------------

    def run(self):
        self.running = True

        while self.running:
            # Don't do work if we've lost communication with the ZK cluster
            while self.zk and (self.zk.suspended or self.zk.lost):
                self.log.info("ZooKeeper suspended. Waiting")
                time.sleep(SUSPEND_WAIT_TIME)

            # Make sure we're always registered with ZK
            self.zk.registerLauncher(self.launcher_id)

            if self.provider.max_concurrency == -1 and self.workers:
                self.workers = []

            if self.provider.max_concurrency != 0:
                self._processRequests()

            time.sleep(10)
            self._updateProvider()

    def stop(self):
        self.log.info("%s received stop" % self.name)
        self.running = False


class NodePool(threading.Thread):
    log = logging.getLogger("nodepool.NodePool")

    def __init__(self, securefile, configfile, no_deletes=False,
                 watermark_sleep=WATERMARK_SLEEP):
        threading.Thread.__init__(self, name='NodePool')
        self.securefile = securefile
        self.configfile = configfile
        self.no_deletes = no_deletes
        self.watermark_sleep = watermark_sleep
        self._stopped = False
        self.config = None
        self.apsched = None
        self.zk = None
        self.statsd = stats.get_client()
        self._delete_threads = {}
        self._delete_threads_lock = threading.Lock()
        self._instance_delete_threads = {}
        self._instance_delete_threads_lock = threading.Lock()
        self._wake_condition = threading.Condition()

    def stop(self):
        self._stopped = True
        self._wake_condition.acquire()
        self._wake_condition.notify()
        self._wake_condition.release()
        if self.config:
            provider_manager.ProviderManager.stopProviders(self.config)
        if self.apsched and self.apsched.running:
            self.apsched.shutdown()
        self.log.debug("finished stopping")

    def loadConfig(self):
        self.log.debug("Loading configuration")
        config = nodepool_config.loadConfig(self.configfile)
        nodepool_config.loadSecureConfig(config, self.securefile)
        return config

    def reconfigureManagers(self, config, check_targets=True):
        provider_manager.ProviderManager.reconfigure(self.config, config)

        stop_managers = []
        for t in config.targets.values():
            oldmanager = None
            if self.config:
                oldmanager = self.config.jenkins_managers.get(t.name)
            if oldmanager:
                if (t.jenkins_url != oldmanager.target.jenkins_url or
                    t.jenkins_user != oldmanager.target.jenkins_user or
                    t.jenkins_apikey != oldmanager.target.jenkins_apikey):
                    stop_managers.append(oldmanager)
                    oldmanager = None
            if oldmanager:
                config.jenkins_managers[t.name] = oldmanager
            elif t.jenkins_url:
                self.log.debug("Creating new JenkinsManager object "
                               "for %s" % t.name)
                config.jenkins_managers[t.name] = \
                    jenkins_manager.JenkinsManager(t)
                config.jenkins_managers[t.name].start()
        for oldmanager in stop_managers:
            oldmanager.stop()

        # only do it if we need to check for targets
        if check_targets:
            for t in config.targets.values():
                if t.jenkins_url:
                    try:
                        info = config.jenkins_managers[t.name].getInfo()
                        if info['quietingDown']:
                            self.log.info("Target %s is offline" % t.name)
                            t.online = False
                        else:
                            t.online = True
                    except Exception:
                        self.log.exception("Unable to check status of %s" %
                                           t.name)
                        t.online = False

    def reconfigureCrons(self, config):
        cron_map = {
            'cleanup': self._doPeriodicCleanup,
            'check': self._doPeriodicCheck,
            }

        if not self.apsched:
            self.apsched = apscheduler.schedulers.background.BackgroundScheduler()
            self.apsched.start()

        for c in config.crons.values():
            if ((not self.config) or
                c.timespec != self.config.crons[c.name].timespec):
                if self.config and self.config.crons[c.name].job:
                    self.config.crons[c.name].job.remove()
                parts = c.timespec.split()
                if len(parts) > 5:
                    second = parts[5]
                else:
                    second = None
                minute, hour, dom, month, dow = parts[:5]
                trigger = apscheduler.triggers.cron.CronTrigger(
                    month=month, day=dom, day_of_week=dow,
                    hour=hour, minute=minute, second=second)
                c.job = self.apsched.add_job(
                    cron_map[c.name], trigger=trigger)
            else:
                c.job = self.config.crons[c.name].job

    def reconfigureZooKeeper(self, config):
        if self.config:
            running = self.config.zookeeper_servers.values()
        else:
            running = None

        configured = config.zookeeper_servers.values()
        if running == configured:
            self.log.debug("Zookeeper client does not need to be updated")
            return

        if not self.zk and configured:
            self.log.debug("Connecting to ZooKeeper servers")
            self.zk = zk.ZooKeeper()
            self.zk.connect(configured)
        else:
            self.log.debug("Detected ZooKeeper server changes")
            self.zk.resetHosts(configured)

    def setConfig(self, config):
        self.config = config

    def getDB(self):
        return self.config.db

    def getZK(self):
        return self.zk

    def getProviderManager(self, provider):
        return self.config.provider_managers[provider.name]

    def getJenkinsManager(self, target):
        if target.name in self.config.jenkins_managers:
            return self.config.jenkins_managers[target.name]
        else:
            raise KeyError("{0} not in {1}".format(target.name,
                           self.config.jenkins_managers.keys()))

    def getNeededNodes(self, session, allocation_history):
        self.log.debug("Beginning node launch calculation")
        # Get the current demand for nodes.
        label_demand = {}

        for name, demand in label_demand.items():
            self.log.debug("  Demand from gearman: %s: %s" % (name, demand))

        online_targets = set()
        for target in self.config.targets.values():
            if not target.online:
                continue
            online_targets.add(target.name)

        nodes = session.getNodes()

        def count_nodes(label_name, state):
            return len([n for n in nodes
                        if (n.target_name in online_targets and
                            n.label_name == label_name and
                            n.state == state)])

        def count_nodes_and_subnodes(provider_name):
            count = 0
            for n in nodes:
                if n.provider_name != provider_name:
                    continue
                count += 1 + len(n.subnodes)
            return count

        # Add a provider for each node provider, along with current
        # capacity
        allocation_providers = {}
        for provider in self.config.providers.values():
            provider_max = provider.max_servers
            n_provider = count_nodes_and_subnodes(provider.name)
            available = provider_max - n_provider
            if available < 0:
                self.log.warning("Provider %s over-allocated: "
                                 "max-servers %d but counted %d " %
                                 (provider.name, provider_max, n_provider))
                available = 0
            ap = allocation.AllocationProvider(provider.name, available)
            allocation_providers[provider.name] = ap

        # calculate demand for labels
        # Actual need is: demand - (ready + building + used)
        # NOTE(jhesketh): This assumes that the nodes in use are actually being
        # used for a job in demand.
        for label in self.config.labels.values():
            start_demand = label_demand.get(label.name, 0)
            n_ready = count_nodes(label.name, nodedb.READY)
            n_building = count_nodes(label.name, nodedb.BUILDING)
            n_used = count_nodes(label.name, nodedb.USED)
            n_test = count_nodes(label.name, nodedb.TEST)
            ready = n_ready + n_building + n_used + n_test

            capacity = 0
            for provider in label.providers.values():
                capacity += allocation_providers[provider.name].available

            # Note actual_demand and extra_demand are written this way
            # because max(0, x - y + z) != max(0, x - y) + z.
            # The requested number of nodes minus those already available
            actual_demand = max(0, start_demand - ready)
            # Demand that accomodates extra demand from min-ready value
            extra_demand = max(0, start_demand + label.min_ready - ready)
            # We only request extras for the min ready value if there is
            # clearly capacity for them. This is to avoid the allocator
            # making bad choices spinning up nodes to satisfy min-ready when
            # there is "real" work to do with other nodes.
            if extra_demand <= capacity:
                demand = extra_demand
            else:
                demand = actual_demand

            label_demand[label.name] = demand
            self.log.debug("  Deficit: %s: %s "
                           "(start: %s min-ready: %s ready: %s capacity: %s)" %
                           (label.name, demand,
                            start_demand, label.min_ready, ready, capacity))

        # "Target-Label-Provider" -- the triplet of info that identifies
        # the source and location of each node.  The mapping is
        # AllocationGrantTarget -> TargetLabelProvider, because
        # the allocation system produces AGTs as the final product.
        tlps = {}
        # label_name -> AllocationRequest
        allocation_requests = {}
        # Set up the request values in the allocation system
        for target in self.config.targets.values():
            if not target.online:
                continue
            at = allocation.AllocationTarget(target.name)
            for label in self.config.labels.values():
                ar = allocation_requests.get(label.name)
                if not ar:
                    # A request for a certain number of nodes of this
                    # label type.  We may have already started a
                    # request from a previous target-label in this
                    # loop.
                    ar = allocation.AllocationRequest(label.name,
                                                      label_demand[label.name],
                                                      allocation_history)

                nodes = session.getNodes(label_name=label.name,
                                         target_name=target.name)
                allocation_requests[label.name] = ar
                ar.addTarget(at, len(nodes))
                for provider in label.providers.values():
                    image = self.zk.getMostRecentImageUpload(
                        label.image, provider.name)
                    if image:
                        # This request may be supplied by this provider
                        # (and nodes from this provider supplying this
                        # request should be distributed to this target).
                        sr, agt = ar.addProvider(
                            allocation_providers[provider.name],
                            at, label.subnodes)
                        tlps[agt] = (target, label,
                                     self.config.providers[provider.name])
                    else:
                        self.log.debug("  %s does not have image %s "
                                       "for label %s." % (provider.name,
                                                          label.image,
                                                          label.name))

        self.log.debug("  Allocation requests:")
        for ar in allocation_requests.values():
            self.log.debug('    %s' % ar)
            for sr in ar.sub_requests.values():
                self.log.debug('      %s' % sr)

        nodes_to_launch = []

        # Let the allocation system do it's thing, and then examine
        # the AGT objects that it produces.
        self.log.debug("  Grants:")
        for ap in allocation_providers.values():
            ap.makeGrants()
            for g in ap.grants:
                self.log.debug('    %s' % g)
                for agt in g.targets:
                    self.log.debug('      %s' % agt)
                    tlp = tlps[agt]
                    nodes_to_launch.append((tlp, agt.amount))

        allocation_history.grantsDone()

        self.log.debug("Finished node launch calculation")
        return nodes_to_launch

    def getNeededSubNodes(self, session):
        nodes_to_launch = []
        for node in session.getNodes():
            if node.label_name in self.config.labels:
                expected_subnodes = \
                    self.config.labels[node.label_name].subnodes
                active_subnodes = len([n for n in node.subnodes
                                       if n.state != nodedb.DELETE])
                deficit = max(expected_subnodes - active_subnodes, 0)
                if deficit:
                    nodes_to_launch.append((node, deficit))
        return nodes_to_launch

    def updateConfig(self):
        config = self.loadConfig()
        self.reconfigureZooKeeper(config)
        self.reconfigureManagers(config)
        self.reconfigureCrons(config)
        self.setConfig(config)

    def run(self):
        '''
        Start point for the NodePool thread.
        '''
        # Provider threads keyed by provider name
        provider_threads = {}

        while not self._stopped:
            try:
                self.updateConfig()

                # Don't do work if we've lost communication with the ZK cluster
                while self.zk and (self.zk.suspended or self.zk.lost):
                    self.log.info("ZooKeeper suspended. Waiting")
                    time.sleep(SUSPEND_WAIT_TIME)

                # Start (or restart) provider threads for each provider in
                # the config. Removing a provider from the config and then
                # adding it back would cause a restart.
                for p in self.config.providers.values():
                    if p.name not in provider_threads.keys():
                        t = ProviderWorker(self.configfile, self.zk, p)
                        self.log.info( "Starting %s" % t.name)
                        t.start()
                        provider_threads[p.name] = t
                    elif not provider_threads[p.name].isAlive():
                        provider_threads[p.name].join()
                        t = ProviderWorker(self.configfile, self.zk, p)
                        self.log.info( "Restarting %s" % t.name)
                        t.start()
                        provider_threads[p.name] = t
            except Exception:
                self.log.exception("Exception in main loop:")

            self._wake_condition.acquire()
            self._wake_condition.wait(self.watermark_sleep)
            self._wake_condition.release()

        # Stop provider threads
        for thd in provider_threads.values():
            if thd.isAlive():
                thd.stop()
            self.log.info("Waiting for %s" % thd.name)
            thd.join()

    def _run(self, session, allocation_history):
        # Make up the subnode deficit first to make sure that an
        # already allocated node has priority in filling its subnodes
        # ahead of new nodes.
        subnodes_to_launch = self.getNeededSubNodes(session)
        for (node, num_to_launch) in subnodes_to_launch:
            self.log.info("Need to launch %s subnodes for node id: %s" %
                          (num_to_launch, node.id))
            for i in range(num_to_launch):
                self.launchSubNode(session, node)

        nodes_to_launch = self.getNeededNodes(session, allocation_history)

        for (tlp, num_to_launch) in nodes_to_launch:
            (target, label, provider) = tlp
            if (not target.online) or (not num_to_launch):
                continue
            self.log.info("Need to launch %s %s nodes for %s on %s" %
                          (num_to_launch, label.name,
                           target.name, provider.name))
            for i in range(num_to_launch):
                cloud_image = self.zk.getMostRecentImageUpload(
                    label.image, provider.name)
                if not cloud_image:
                    self.log.debug("No current image for %s on %s"
                                   % (label.image, provider.name))
                else:
                    self.launchNode(session, provider, label, target)

    def launchNode(self, session, provider, label, target):
        try:
            self._launchNode(session, provider, label, target)
        except Exception:
            self.log.exception(
                "Could not launch node %s on %s", label.name, provider.name)

    def _launchNode(self, session, provider, label, target):
        provider = self.config.providers[provider.name]
        timeout = provider.boot_timeout
        launch_timeout = provider.launch_timeout
        if provider.azs:
            az = random.choice(provider.azs)
        else:
            az = None
        node = session.createNode(provider.name, label.name, target.name, az)
        t = NodeLauncher(self, provider, label, target, node.id, timeout,
                         launch_timeout)
        t.start()

    def launchSubNode(self, session, node):
        try:
            self._launchSubNode(session, node)
        except Exception:
            self.log.exception(
                "Could not launch subnode for node id: %s", node.id)

    def _launchSubNode(self, session, node):
        provider = self.config.providers[node.provider_name]
        label = self.config.labels[node.label_name]
        timeout = provider.boot_timeout
        launch_timeout = provider.launch_timeout
        subnode = session.createSubNode(node)
        t = SubNodeLauncher(self, provider, label, subnode.id,
                            node.id, node.target_name, timeout, launch_timeout,
                            node_az=node.az, manager_name=node.manager_name)
        t.start()

    def deleteSubNode(self, subnode, manager):
        # Don't try too hard here, the actual node deletion will make
        # sure this is cleaned up.
        if subnode.external_id:
            try:
                self.log.debug('Deleting server %s for subnode id: '
                               '%s of node id: %s' %
                               (subnode.external_id, subnode.id,
                                subnode.node.id))
                manager.cleanupServer(subnode.external_id)
                manager.waitForServerDeletion(subnode.external_id)
            except provider_manager.NotFound:
                pass
        subnode.delete()

    def deleteNode(self, node_id):
        try:
            self._delete_threads_lock.acquire()
            if node_id in self._delete_threads:
                return
            t = NodeDeleter(self, node_id)
            self._delete_threads[node_id] = t
            t.start()
        except Exception:
            self.log.exception("Could not delete node %s", node_id)
        finally:
            self._delete_threads_lock.release()

    def _deleteNode(self, session, node):
        self.log.debug("Deleting node id: %s which has been in %s "
                       "state for %s hours" %
                       (node.id, nodedb.STATE_NAMES[node.state],
                        (time.time() - node.state_time) / (60 * 60)))
        # Delete a node
        if node.state != nodedb.DELETE:
            # Don't write to the session if not needed.
            node.state = nodedb.DELETE
        self.updateStats(session, node.provider_name)
        provider = self.config.providers[node.provider_name]
        target = self.config.targets[node.target_name]
        label = self.config.labels.get(node.label_name, None)
        if label and label.image in provider.images:
            image_name = provider.images[label.image].name
        else:
            image_name = None
        manager = self.getProviderManager(provider)

        if target.jenkins_url and (node.nodename is not None):
            jenkins = self.getJenkinsManager(target)
            jenkins_name = node.nodename
            if jenkins.nodeExists(jenkins_name):
                jenkins.deleteNode(jenkins_name)
            self.log.info("Deleted jenkins node id: %s" % node.id)

        if node.manager_name is not None:
            try:
                self.revokeAssignedNode(node)
            except Exception:
                self.log.exception("Exception revoking node id: %s" %
                                   node.id)

        for subnode in node.subnodes:
            if subnode.external_id:
                try:
                    self.log.debug('Deleting server %s for subnode id: '
                                   '%s of node id: %s' %
                                   (subnode.external_id, subnode.id, node.id))
                    manager.cleanupServer(subnode.external_id)
                except provider_manager.NotFound:
                    pass

        if node.external_id:
            try:
                self.log.debug('Deleting server %s for node id: %s' %
                               (node.external_id, node.id))
                manager.cleanupServer(node.external_id)
                manager.waitForServerDeletion(node.external_id)
            except provider_manager.NotFound:
                pass
            node.external_id = None

        for subnode in node.subnodes:
            if subnode.external_id:
                manager.waitForServerDeletion(subnode.external_id)
            subnode.delete()

        node.delete()
        self.log.info("Deleted node id: %s" % node.id)

        if self.statsd:
            dt = int((time.time() - node.state_time) * 1000)
            key = 'nodepool.delete.%s.%s.%s' % (image_name,
                                                node.provider_name,
                                                node.target_name)
            self.statsd.timing(key, dt)
            self.statsd.incr(key)
        self.updateStats(session, node.provider_name)

    def deleteInstance(self, provider_name, external_id):
        key = (provider_name, external_id)
        try:
            self._instance_delete_threads_lock.acquire()
            if key in self._instance_delete_threads:
                return
            t = InstanceDeleter(self, provider_name, external_id)
            self._instance_delete_threads[key] = t
            t.start()
        except Exception:
            self.log.exception("Could not delete instance %s on provider %s",
                               provider_name, external_id)
        finally:
            self._instance_delete_threads_lock.release()

    def _deleteInstance(self, provider_name, external_id):
        provider = self.config.providers[provider_name]
        manager = self.getProviderManager(provider)
        manager.cleanupServer(external_id)

    def _doPeriodicCleanup(self):
        if self.no_deletes:
            return
        try:
            self.periodicCleanup()
        except Exception:
            self.log.exception("Exception in periodic cleanup:")

    def periodicCleanup(self):
        # This function should be run periodically to clean up any hosts
        # that may have slipped through the cracks, as well as to remove
        # old images.

        self.log.debug("Starting periodic cleanup")

        for k, t in self._delete_threads.items()[:]:
            if not t.isAlive():
                del self._delete_threads[k]

        for k, t in self._instance_delete_threads.items()[:]:
            if not t.isAlive():
                del self._instance_delete_threads[k]

        node_ids = []
        with self.getDB().getSession() as session:
            for node in session.getNodes():
                node_ids.append(node.id)

        for node_id in node_ids:
            try:
                with self.getDB().getSession() as session:
                    node = session.getNode(node_id)
                    if node:
                        self.cleanupOneNode(session, node)
            except Exception:
                self.log.exception("Exception cleaning up node id %s:" %
                                   node_id)

        try:
            self.cleanupLeakedInstances()
            pass
        except Exception:
            self.log.exception("Exception cleaning up leaked nodes")

        self.log.debug("Finished periodic cleanup")

    def cleanupLeakedInstances(self):
        known_providers = self.config.providers.keys()
        for provider in self.config.providers.values():
            manager = self.getProviderManager(provider)
            servers = manager.listServers()
            with self.getDB().getSession() as session:
                for server in servers:
                    meta = server.get('metadata', {}).get('nodepool')
                    if not meta:
                        self.log.debug("Instance %s (%s) in %s has no "
                                       "nodepool metadata" % (
                                           server['name'], server['id'],
                                           provider.name))
                        continue
                    meta = json.loads(meta)
                    if meta['provider_name'] not in known_providers:
                        self.log.debug("Instance %s (%s) in %s "
                                       "lists unknown provider %s" % (
                                           server['name'], server['id'],
                                           provider.name,
                                           meta['provider_name']))
                        continue
                    node_id = meta.get('node_id')
                    if node_id:
                        if session.getNode(node_id):
                            continue
                        self.log.warning("Deleting leaked instance %s (%s) "
                                         "in %s for node id: %s" % (
                                             server['name'], server['id'],
                                             provider.name, node_id))
                        self.deleteInstance(provider.name, server['id'])
                    else:
                        self.log.warning("Instance %s (%s) in %s has no "
                                         "database id" % (
                                             server['name'], server['id'],
                                             provider.name))
                        continue
            if provider.clean_floating_ips:
                manager.cleanupLeakedFloaters()

    def cleanupOneNode(self, session, node):
        now = time.time()
        time_in_state = now - node.state_time
        if (node.state in [nodedb.READY, nodedb.HOLD]):
            return
        delete = False
        if (node.state == nodedb.DELETE):
            delete = True
        elif (node.state == nodedb.TEST and
              time_in_state > TEST_CLEANUP):
            delete = True
        elif time_in_state > NODE_CLEANUP:
            delete = True
        if delete:
            try:
                self.deleteNode(node.id)
            except Exception:
                self.log.exception("Exception deleting node id: "
                                   "%s" % node.id)

    def _doPeriodicCheck(self):
        if self.no_deletes:
            return
        try:
            with self.getDB().getSession() as session:
                self.periodicCheck(session)
        except Exception:
            self.log.exception("Exception in periodic check:")

    def periodicCheck(self, session):
        # This function should be run periodically to make sure we can
        # still access hosts via ssh.

        self.log.debug("Starting periodic check")
        for node in session.getNodes():
            if node.state != nodedb.READY:
                continue
            provider = self.config.providers[node.provider_name]
            if node.label_name in self.config.labels:
                label = self.config.labels[node.label_name]
                image = provider.images[label.image]
                connect_kwargs = dict(key_filename=image.private_key)
                try:
                    if utils.ssh_connect(node.ip, image.username,
                                         connect_kwargs=connect_kwargs):
                        continue
                except Exception:
                    self.log.exception("SSH Check failed for node id: %s" %
                                       node.id)
            else:
                self.log.exception("Node with non-existing label %s" %
                                   node.label_name)
            self.deleteNode(node.id)
        self.log.debug("Finished periodic check")

    def updateStats(self, session, provider_name):
        if not self.statsd:
            return
        # This may be called outside of the main thread.

        states = {}

        #nodepool.nodes.STATE
        #nodepool.target.TARGET.nodes.STATE
        #nodepool.label.LABEL.nodes.STATE
        #nodepool.provider.PROVIDER.nodes.STATE
        for state in nodedb.STATE_NAMES.values():
            key = 'nodepool.nodes.%s' % state
            states[key] = 0
            for target in self.config.targets.values():
                key = 'nodepool.target.%s.nodes.%s' % (
                    target.name, state)
                states[key] = 0
            for label in self.config.labels.values():
                key = 'nodepool.label.%s.nodes.%s' % (
                    label.name, state)
                states[key] = 0
            for provider in self.config.providers.values():
                key = 'nodepool.provider.%s.nodes.%s' % (
                    provider.name, state)
                states[key] = 0

        managers = set()

        for node in session.getNodes():
            if node.state not in nodedb.STATE_NAMES:
                continue
            state = nodedb.STATE_NAMES[node.state]
            key = 'nodepool.nodes.%s' % state
            total_nodes = self.config.labels[node.label_name].subnodes + 1
            states[key] += total_nodes

            # NOTE(pabelanger): Check if we assign nodes via Gearman if so, use
            # the manager name.
            #nodepool.manager.MANAGER.nodes.STATE
            if node.manager_name:
                key = 'nodepool.manager.%s.nodes.%s' % (
                    node.manager_name, state)
                if key not in states:
                    states[key] = 0
                managers.add(node.manager_name)
            else:
                key = 'nodepool.target.%s.nodes.%s' % (
                    node.target_name, state)
            states[key] += total_nodes

            key = 'nodepool.label.%s.nodes.%s' % (
                node.label_name, state)
            states[key] += total_nodes

            key = 'nodepool.provider.%s.nodes.%s' % (
                node.provider_name, state)
            states[key] += total_nodes

        # NOTE(pabelanger): Initialize other state values to zero if missed
        # above.
        #nodepool.manager.MANAGER.nodes.STATE
        for state in nodedb.STATE_NAMES.values():
            for manager_name in managers:
                key = 'nodepool.manager.%s.nodes.%s' % (
                    manager_name, state)
                if key not in states:
                    states[key] = 0

        for key, count in states.items():
            self.statsd.gauge(key, count)

        #nodepool.provider.PROVIDER.max_servers
        for provider in self.config.providers.values():
            key = 'nodepool.provider.%s.max_servers' % provider.name
            self.statsd.gauge(key, provider.max_servers)

    def launchStats(self, subkey, dt, image_name,
                    provider_name, target_name, node_az, manager_name):
        if not self.statsd:
            return
        #nodepool.launch.provider.PROVIDER.subkey
        #nodepool.launch.image.IMAGE.subkey
        #nodepool.launch.subkey
        keys = [
            'nodepool.launch.provider.%s.%s' % (provider_name, subkey),
            'nodepool.launch.image.%s.%s' % (image_name, subkey),
            'nodepool.launch.%s' % (subkey,),
            ]
        if node_az:
            #nodepool.launch.provider.PROVIDER.AZ.subkey
            keys.append('nodepool.launch.provider.%s.%s.%s' %
                        (provider_name, node_az, subkey))

        if manager_name:
            # NOTE(pabelanger): Check if we assign nodes via Gearman if so, use
            # the manager name.
            #nodepool.launch.manager.MANAGER.subkey
            keys.append('nodepool.launch.manager.%s.%s' %
                        (manager_name, subkey))
        else:
            #nodepool.launch.target.TARGET.subkey
            keys.append('nodepool.launch.target.%s.%s' %
                        (target_name, subkey))

        for key in keys:
            self.statsd.timing(key, dt)
            self.statsd.incr(key)
