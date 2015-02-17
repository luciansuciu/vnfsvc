# Copyright (c) 2014 Tata Consultancy Services Limited(TCSL). 
# Copyright 2012 OpenStack Foundation
# All Rights Reserved.
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

import os
import sys
import threading
import time
import yaml
import itertools
import eventlet
eventlet.monkey_patch()

from oslo.config import cfg
from oslo import messaging

from vnfsvc import config
from vnfsvc import context
from vnfsvc import manager
from vnfsvc.common import constants
from vnfsvc.openstack.common import loopingcall
from vnfsvc.openstack.common import service
from vnfsvc.openstack.common import log as logging
from vnfsvc.openstack.common.gettextutils import _

from vnfsvc.agent import rpc as agent_rpc
from vnfsvc.common import config as common_config
from vnfsvc.common import rpc as v_rpc
from vnfsvc.common import topics
from vnfsvc import service as vnfsvc_service
from vnfsvc.openstack.common import uuidutils
from vnfsvc.openstack.common import importutils
from vnfsvc.common import exceptions


LOG = logging.getLogger(__name__)

command_opts = [cfg.StrOpt('uuid', default=None, 
                help='VNF manager identifier'),
                cfg.StrOpt('vnfm-conf-dir', default=None,  
                help='VNF manager identifier')]
cfg.CONF.register_cli_opts(command_opts)

AGENT_VNF_MANAGER = 'VNF manager agent'

class ImplThread(threading.Thread):
    def __init__(self, target, *args):
        self._target = target
        self._args = args
        threading.Thread.__init__(self)

    def run(self):
        self._target(*self._args)

class VNFManager(manager.Manager):

    def __init__(self, host=None):
        super(VNFManager, self).__init__(host=host)
        self.vplugin_rpc = VNFPluginCallbacks(topics.PLUGIN,
                                             cfg.CONF.host)
        self.needs_resync_reasons = []
        self.conf = cfg.CONF
        ctx = context.get_admin_context_without_session()
        self.ns_config = self.conf.vnfm_conf_d['service']
        self.drv_conf = self._extract_drivers()
        self.launched_devs = list()
        self.configure_vdus(ctx)

 
    def _extract_drivers(self):
        vnfds = list(set(self.ns_config.keys()) - set(['id','fg']))
        vnfd_details = dict()
        for vnfd in vnfds:
            for vdu in range(0,len(self.ns_config[vnfd])):
                instances = self.ns_config[vnfd][vdu]['instance_list']
                vdu_name = self.ns_config[vnfd][vdu]['name']
                vnfd_details[vdu_name] = dict()
                vnfd_details[vdu_name]['_driver'] = self.ns_config[vnfd][vdu]['mgmt-driver'] if self.ns_config[vnfd][vdu]['mgmt-driver'] is not '' else None
                vnfd_details[vdu_name]['_lc_events'] = self.ns_config[vnfd][vdu]['lifecycle_events']
                vnfd_details[vdu_name]['_vnfd'] = vnfd
                vnfd_details[vdu_name]['_instances'] = instances
                vnfd_details[vdu_name]['idx'] = vdu
        return vnfd_details


    def _populate_vnfd_drivers(self, drv_conf):
        vnfd_drv_cls_path = drv_conf['_driver']
        try:
            drv_conf['_drv_obj'] = importutils.import_object(vnfd_drv_cls_path,
                                                             self.ns_config)
        except Exception:
            LOG.warn(_("VNF Driver not Loaded"))
            raise


    def _configure_service(self, vdu, instance):
        try:
            vdu['_drv_obj'].configure_service(instance)

        except exceptions.DriverException:
            LOG.exception(_("Driver Exception: Configuration of VNF Failed!!"))


    def _get_vdu_from_conf(self, conf):
        return conf[conf.keys()[0]][0]['name']


    def configure_vdus(self, context, conf=None):
        status_lst = list()
        serv_state = ""
        drv_conf = dict()
        new_drv_conf = list()

        if conf is not None:
            self.ns_config.update(conf['service'])

        curr_drv_conf = self._extract_drivers()
        new_drv_conf = list(set(curr_drv_conf.keys()) - set(self.drv_conf.keys()))

        if new_drv_conf:
            drv_conf = curr_drv_conf
            self.drv_conf.update(curr_drv_conf)
        else:
            new_drv_conf = self.drv_conf.keys()
            drv_conf = self.drv_conf

        for vdu in new_drv_conf:
            if not drv_conf[vdu]['_driver']:
               continue
            self._populate_vnfd_drivers(drv_conf[vdu])
        
        for vdu_name in new_drv_conf:
            self._configure_vdu(context, vdu_name)


    def _configure_vdu(self, context, vdu_name):
        status = ""
        for instance in range(0,len(self.drv_conf[vdu_name]['_instances'])):
            if self.drv_conf[vdu_name]['_driver']:
                status = self._invoke_driver_thread(self.drv_conf[vdu_name],
                                   self.drv_conf[vdu_name]['_instances'][instance])

                if status == 'COMPLETE':
                    self.launched_devs.extend(self.drv_conf[vdu_name]['_instances'][instance])

                # Sending acknowledgement to VNFPlugin, notifying that the configuration
                # of VNF has completed/errored out by its drvier.
                self.vplugin_rpc.send_ack(context, self.drv_conf[vdu_name]['_vnfd'], vdu_name,
                                     self.drv_conf[vdu_name]['_instances'][instance], status)
            else:
                # If no driver has specified for the vdu.
                self.launched_devs.extend(self.drv_conf[vdu_name]['_instances'])
                status = 'COMPLETE'
                self.vplugin_rpc.send_ack(context, self.drv_conf[vdu_name]['_vnfd'], vdu_name, 
                                     self.drv_conf[vdu_name]['_instances'][instance], status)


    def _invoke_driver_thread(self, vdu, instance):
        LOG.debug(_("Configuration of the remote VNF %s being intiated"), instance)
        try:
           driver_thread = ImplThread(self._configure_service, vdu, instance)
           driver_thread.start()
           status = "COMPLETE"
        except exceptions.DriverException or Exception:
           LOG.warning(_("Configuration of VNF by the Driver Failed!"))
           status = "ERROR"
        driver_thread.join()

        if driver_thread.isAlive():
           driver_thread.kill()

        return status


class VNFPluginCallbacks(v_rpc.RpcProxy):
    """Manager side of the vnf manager to vnf Plugin RPC API."""

    def __init__(self, topic, host):
        RPC_API_VERSION = '1.0' 
        super(VNFPluginCallbacks, self).__init__(topic, RPC_API_VERSION)

    def send_ack(self, context, vnfd, vdu, instance, status):
        return self.call(context,
                         self.make_msg('send_ack', vnfd=vnfd, vdu=vdu, instance=instance, status=status))


class VNFMgrWithStateReport(VNFManager):
    def __init__(self, host=None):
        super(VNFMgrWithStateReport, self).__init__(host=cfg.CONF.host)
        self.state_rpc = agent_rpc.PluginReportStateAPI(topics.PLUGIN)
        self.agent_state = { 
            'binary': 'vnf-manager',
            'host': host,
            'topic': topics.set_topic_name(self.conf.uuid, prefix=topics.VNF_MANAGER),
            'configurations': {
                'agent_status': 'ACTIVE',
                'agent_id': cfg.CONF.uuid
                },
            'start_flag': True,
            'agent_type': constants.AGENT_VNF_MANAGER}
        report_interval = 60                    # cfg.CONF.AGENT.report_interval. Hardcoded for time-while
                                                # However, they can be easily migrated to conf file.
        self.use_call = True
        if report_interval:
            self.heartbeat = loopingcall.FixedIntervalLoopingCall(
                self._report_state)
            self.heartbeat.start(interval=report_interval)


    def _report_state(self):
        try:
            self.agent_state.get('configurations').update(
                self.cache.get_state())
            ctx = context.get_admin_context_without_session()
            self.state_rpc.report_state(ctx, self.agent_state, self.use_call)
            #self.use_call = False
        except AttributeError:
            # This means the server does not support report_state
            LOG.warn(_("VNF server does not support state report."
                       " State report for this agent will be disabled."))
            self.heartbeat.stop()
            return
        except Exception:
            LOG.exception(_("Failed reporting state!"))
            return


class ImplThread(threading.Thread):
    def __init__(self, target, *args):
        self._target = target
        self._args = args
        threading.Thread.__init__(self)

    def run(self):
        self._target(*self._args)


def load_vnfm_conf(conf_path):
    conf_doc = open(conf_path, 'r')
    conf_dict = yaml.load(conf_doc)
    OPTS = [cfg.DictOpt('vnfm_conf_d', default=conf_dict)]
    cfg.CONF.register_opts(OPTS)


def _register_opts(conf):
    config.register_agent_state_opts_helper(conf)
    config.register_root_helper(conf)


def read_sys_args(arg_list):
    """ To be implemented.

      Reads a command-line arguments and returns a dict 
      for easier processing of cmd args and useful when 
      a number of args need to specified for the service. """
    arg_dict = dict()
    rg = len(arg_list[1:0])/2
    for ele in range(0, rg):
        arg_dict[arg_list[ele]] = arg_list[arg_list[ele+1]]
        del(arg_list[ele],arg_list[ele+1])
    return arg_dict


def main(manager='vnfsvc.vnfmanager.manager.VNFMgrWithStateReport'):
    _register_opts(cfg.CONF)
    common_config.init(sys.argv[1:])
    uuid = sys.argv[-1]
    config.setup_logging(cfg.CONF)
    LOG.warn(_("UUID: %s"), uuid)
    vnfm_conf_path = sys.argv[4:5][0]+uuid+'.yaml'
    load_vnfm_conf(vnfm_conf_path)

    server = vnfsvc_service.Service.create(
        binary='vnf-manager',
        topic=topics.set_topic_name(uuid, prefix=topics.VNF_MANAGER),
        report_interval=60,
        manager=manager)
    service.launch(server).wait()
