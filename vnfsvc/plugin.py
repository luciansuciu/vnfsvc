# Copyright 2014 Tata Consultancy Services Ltd.
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

import json
import os
import uuid
import shutil
import six
import subprocess
import eventlet
import pexpect
import tarfile
import time
import sys 
import re
import yaml
import ast

from collections import OrderedDict
from distutils import dir_util
from netaddr import IPAddress, IPNetwork
from oslo.config import cfg

from vnfsvc.agent.linux import ovs_lib

from vnfsvc import constants
from vnfsvc import manager
from vnfsvc import constants as vm_constants
from vnfsvc import config
from vnfsvc import context
from vnfsvc import nsdmanager

from vnfsvc.api.v2 import attributes
from vnfsvc.api.v2 import vnf
from vnfsvc.db.vnf import vnf_db
from vnfsvc import vnffg

from vnfsvc.openstack.common.gettextutils import _
from vnfsvc.openstack.common import excutils
from vnfsvc.openstack.common import log as logging
from vnfsvc.openstack.common import importutils

from vnfsvc.client import client

from vnfsvc.common import driver_manager
from vnfsvc.common import exceptions
from vnfsvc.common import rpc as v_rpc
from vnfsvc.common import topics
from vnfsvc.common import utils
from vnfsvc.common.yaml.nsdparser import NetworkParser
from vnfsvc.common.yaml.vnfdparser import VNFParser

LOG = logging.getLogger(__name__)


class VNFPlugin(vnf_db.NetworkServicePluginDb):
    """VNFPlugin which provide support to OpenVNF framework"""

    #register vnf driver 
    OPTS = [
        cfg.MultiStrOpt(
            'vnf_driver', default=[],
            help=_('Hosting  drivers for vnf will use')),
        cfg.StrOpt(
            'templates', default='',
            help=_('Path to service templates')),
        cfg.StrOpt(
            'vnfmanager', default='',
            help=_('Path to VNF Manager')),
        cfg.StrOpt(
            'compute_hostname', default='',
            help=_('Compute Hostname')),
        cfg.StrOpt(
            'compute_user', default='',
            help=_('User name')),
        cfg.StrOpt(
            'vnfm_home_dir', default='',
            help=_('vnf_home_dir')),
        cfg.StrOpt(
            'ssh_pwd', default='',
            help=_('ssh_pwd')),
        cfg.StrOpt(
            'ovs_bridge', default='br-int',
            help=_('ovs_bridge')),
        cfg.StrOpt(
            'neutron_rootwrap', default='',
            help=_('path to neutron rootwrap')),
        cfg.StrOpt(
            'neutron_rootwrapconf', default='',
            help=_('path to neutron rootwrap conf')),
        cfg.StrOpt(
            'vnfmconf', default='local',
            help=_('Vnf Manager Configuaration')),
    ]
    cfg.CONF.register_opts(OPTS, 'vnf')
    conf = cfg.CONF

    def __init__(self):
        super(VNFPlugin, self).__init__()
        self.novaclient = client.NovaClient()
        self.glanceclient = client.GlanceClient()
        self.neutronclient = client.NeutronClient()
        self._pool = eventlet.GreenPool()
        self.conf = cfg.CONF
        self.is_manager_invoked =  False
        self.nsd_template = dict()

        config.register_root_helper(self.conf)
        self.root_helper = config.get_root_helper(self.conf)
        self.agent_mapping = dict()

        self.endpoints = [VNFManagerCallbacks(self)]
        self.conn = v_rpc.create_connection(new=True)
        self.conn.create_consumer(
            topics.PLUGIN, self.endpoints, fanout=False)

        self.conn.consume_in_threads()


    def spawn_n(self, function, *args, **kwargs):
        self._pool.spawn_n(function, *args, **kwargs)


    def create_service(self, context, service):
        self.vnfds = {}
        self.instances = {}
        self.created = []
        self.conf_generated = []
        self.vnfmanager_uuid = str(uuid.uuid4())
        self.nsd_id = str(uuid.uuid4())
        self.acknowledge_list = dict()
        self.deployed_vdus = list()
        self.vnfm_dir =  self.conf.state_path+'/'+self.vnfmanager_uuid
        if not os.path.exists(self.vnfm_dir):
            os.makedirs(self.vnfm_dir)

        self.service = service['service']
        self.networks = self.service['attributes']['networks']
        self.router = self.service['attributes']['router']
        self.subnets = self.service['attributes']['subnets']
        self.service_name = self.service['name']
        self.qos = self.service['quality_of_service']
        self.templates_json = json.load(open(self.conf.vnf.templates, 'r'))
        self.nsd_template = yaml.load(open(self.templates_json['nsd'][self.service_name], 'r'))['nsd']
        self.nsd_template = NetworkParser(self.nsd_template).parse(self.qos, self.networks,
                                self.router, self.subnets)
        self.nsd_template['router'] = {}
        self.nsd_template = nsdmanager.Configuration(self.nsd_template).preconfigure()
        for vnfd in self.nsd_template['vnfds']:
            vnfd_template =  yaml.load(open(self.templates_json['vnfd'][vnfd], 'r'))
            self.vnfds[vnfd] = dict()
            self.vnfds[vnfd]['template'] = vnfd_template
            self.vnfds[vnfd] = VNFParser(self.vnfds[vnfd],self.qos,
                                self.nsd_template['vnfds'][vnfd], vnfd, self.nsd_template).parse()
            self.vnfds[vnfd]['vnf_id'] = str(uuid.uuid4())

        db_dict = {
            'id': self.nsd_id,
            'nsd': self.nsd_template,
            'vnfds': self.vnfds,
            'networks': self.networks,
            'subnets': self.subnets,
            'vnfm_id': self.vnfmanager_uuid,
            'service': self.service,
            'status': 'PENDING'
        }
        #Create DB Entry for the new service
        ns_dict = self.create_service_model(context, **db_dict)

        #Launch VNFDs
        self._create_vnfds(context)

        #TODO : (tcs) Need to enhance computation of forwarding graph
        vnffg.ForwardingGraph(self.nsd_template, self.vnfds).configure_forwarding_graph()

        self.update_nsd_status(context, self.nsd_id, 'ACTIVE')
        return ns_dict


    def delete_service(self,context,service):
        body={}
        instances=[]
        fixed_ips=[]
        subnet_ids=[]
        service_db_dict=self.delete_service_model(context,service)
        net_ids=ast.literal_eval(service_db_dict['service_db'][0].__dict__['networks']).values()
        router_id=ast.literal_eval(service_db_dict['service_db'][0].__dict__['router'])['id']
        #puppet_id=service_db_dict['service_db'][0]['puppet_id']
        #self.novaclient.delete(puppet_id)
        try:
           for instance in range(len(service_db_dict['instances'])):
               instances.append(service_db_dict['instances'][instance][0].__dict__['instances'].split(','))
               self.novaclient.delete_flavor(service_db_dict['instances'][instance][0].__dict__['flavor'])
               self.glanceclient.delete_image(service_db_dict['instances'][instance][0].__dict__['image'])
           for inst in instances:
               for prop in range(len(inst)):
                   self.novaclient.delete(inst[prop])

           time.sleep(15)
           router_ports=self.neutronclient.list_router_ports(router_id)
           for r_port in range(len(router_ports['ports'])):
               fixed_ips.append(router_ports['ports'][r_port]['fixed_ips'])
 
           for ip in range(len(fixed_ips)):
               subnet_ids.append(fixed_ips[ip][0]['subnet_id'])
 
           for s_id in range(len(subnet_ids)):
               body['subnet_id']=subnet_ids[s_id]
               self.neutronclient.remove_interface_router(router_id, body)
           port_list=self.neutronclient.list_ports()

           for port in range(len(port_list['ports'])):
               if port_list['ports'][port]['network_id'] in net_ids:
                  port_id = port_list['ports'][port]['id']
                  self.neutronclient.delete_port(port_id)
           for j in range(len(net_ids)):
               self.neutronclient.delete_network(net_ids[j])
           self.delete_db_dict(context,service)
        except Exception:
           pass


    def _get_vnfds_no_dependency(self):
        """ Returns all the vnfds which don't have dependency """
        temp_vnfds = list()
        for vnfd in self.nsd_template['vnfds']:
            for vdu in self.nsd_template['vnfds'][vnfd]:
                if 'dependency' not in self.nsd_template['vdus'][vnfd+':'+vdu].keys():
                    temp_vnfds.append(vnfd+':'+vdu)
        return temp_vnfds

    
    def _create_flavor(self, vnfd, vdu):
        """ Create a openstack flavor based on vnfd flavor """
        flavor_dict = VNFParser().get_flavor_dict(self.vnfds[vnfd]['vdus'][vdu])
        flavor_dict['name'] = vnfd+'_'+vdu+flavor_dict['name']
        return self.novaclient.create_flavor(**flavor_dict)


    def _create_vnfds(self, context):
        ivnfds = self._get_vnfds_no_dependency()

        """ Deploy independent VNF/VNF'S """
        for vnfd in ivnfds:
	    self._launch_vnfds(vnfd, context)

        self.diff_vdus = [vdu for vdu in self.nsd_template['vdus'].keys() if vdu not in ivnfds]

        self._invoke_vnf_manager(context)


    def _resolve_dependency(self, context):
        while len(self.created) != len(self.nsd_template['vdus'].keys()):
            for vnfd in self.diff_vdus:
                if len(self.created) == len(self.nsd_template['vdus'].keys()):
                    break
                vnfd_name, vdu_name = vnfd.split(':')[0],vnfd.split(':')[1]
                dependency = self.nsd_template['vdus'][vnfd_name+':'+vdu_name]['dependency']
                if set(dependency) <= set(self.created) and vnfd not in self.deployed_vdus:
                    self._launch_vnfds(vnfd, context)
                    conf = self._generate_vnfm_conf()
                    self.conf_generated.append(vnfd)
                    self.agent_mapping[self.vnfmanager_uuid].configure_vdus(context, conf=conf)
 		elif set(dependency) <= set(self.deployed_vdus) and \
                     not set(dependency) <= set(self.created):
                    LOG.debug(_('Waiting for ack from VNF Manager of %s'),dependency)
                    time.sleep(3)
                else:
                    time.sleep(3)
                    continue
                self.diff_vdus = list(set(self.diff_vdus) - set(self.created))
    
    def _launch_vnfds(self, vnfd, context):
        """
        1)create conf dict
        2)send conf dict to VNF Manager using RPC if VNF Manager was already invoked
        """
        vnfd_name, vdu_name = vnfd.split(':')[0],vnfd.split(':')[1]
        flavor = self._create_flavor(vnfd_name, vdu_name)
        self.vnfds[vnfd_name]['vdus'][vdu_name]['new_flavor'] = flavor.id
        name = vnfd_name.lower()+'-'+vdu_name.lower()
        vm_details = VNFParser().get_boot_details(self.vnfds[vnfd_name]['vdus'][vdu_name])
        vm_details['name'] = name
        vm_details['flavor'] = flavor
        image_details = self.vnfds[vnfd_name]['vdus'][vdu_name]['vm_details']['image_details']
        if 'image-id' in image_details.keys():
            image = self.glanceclient.get_image(image_details['image-id'])
        else:
            image_details['data'] = open(image_details['image'], 'rb')
            image = self.glanceclient.create_image(**image_details)
            while image.status!='active':
                time.sleep(5)
                image = self.glanceclient.get_image(image.id)
        self.vnfds[vnfd_name]['vdus'][vdu_name]['new_img'] = image.id
        for key in image_details.keys():
            if key not in ['username', 'password']:
                del image_details[key]
        vm_details['image_created'] = image
        nics = []
        ni = self.vnfds[vnfd_name]['vdus'][vdu_name]['vm_details']['network_interfaces']
        for key in ni:
            if 'port_id' in ni[key].keys():
               nics.append({"subnet-id":ni[key]['subnet-id'],
                    "net-id": ni[key]['net-id'],
                    "port-id": ni[key]['port_id']})
            else:
                nics.append({"subnet-id":ni[key]['subnet-id'], "net-id": ni[key]['net-id']})
        mgmt_id =  self.networks['mgmt-if']
        for i in range(len(nics)):
            if nics[i]['net-id'] == mgmt_id:
               temp_net = nics[i]
               del nics[i]
               nics.insert(0, temp_net)
               break
        vm_details['nics'] = nics

        vm_details['userdata'] = self.set_default_userdata(vm_details)
        self.vnfds[vnfd_name]['vdus'][vdu_name]['vm_details']['userdata'] = vm_details['userdata']
        if vnfd_name == 'loadbalancer':
            self.set_default_userdata_loadbalancer(vm_details)


        with open(vm_details['userdata'], 'r') as ud_file:
            data = ud_file.readlines()
        data.insert(0, '#cloud-config\n')
        with open(vm_details['userdata'], 'w') as ud_file:
            ud_file.writelines(data)

        # Update flavor and image details for the vdu
        self.update_vdu_details(context, flavor.id, image.id,
            self.nsd_template['vdus'][vnfd_name+':'+vdu_name]['id'])

        deployed_vdus = self._boot_vdu(context, vnfd, **vm_details)
        if type(deployed_vdus) == type([]):
            self.vnfds[vnfd_name]['vdus'][vdu_name]['instances'] = deployed_vdus
        else:
            self.vnfds[vnfd_name]['vdus'][vdu_name]['instances'] = [deployed_vdus]
        self.vnfds[vnfd_name]['vdus'][vdu_name]['instance_list'] = []

        # Create dictionary with vdu and it's corresponding nova instance ID details
        self._populate_instances_id(vnfd_name, vdu_name)

        for instance in self.vnfds[vnfd_name]['vdus'][vdu_name]['instances']:
            name = instance.name
            self.vnfds[vnfd_name]['vdus'][vdu_name]['instance_list'].append(name)

        self.deployed_vdus.append(vnfd)
        self._set_mgmt_ip(vnfd_name, vdu_name)
        self._set_instance_ip(vnfd_name, vdu_name)


    def set_default_userdata(self, vm_details):
        #TODO: (tcs) Need to enhance regarding puppet installation
        #puppet_master_ip = self.nsd_template['puppet-master']['master-ip']
        #puppet_master_hostname = self.nsd_template['puppet-master']['master-hostname']
        temp_dict = {'runcmd':[], 'manage_etc_hosts': 'localhost'}
        temp_dict['runcmd'].append('dhclient eth1')
        #temp_dict['runcmd'].append('sudo echo '+puppet_master_ip+' '+puppet_master_hostname+' >> /etc/hosts')

        if 'userdata' in vm_details.keys() and vm_details['userdata'] != "":
            with open(vm_details['userdata'], 'r') as f:
                data = yaml.safe_load(f)
            if 'runcmd' in data.keys():
                temp_dict['runcmd'].extend(data['runcmd'])
                data['runcmd'] = temp_dict['runcmd']
            else:
                data['runcmd'] = temp_dict['runcmd']
            data['manage_etc_hosts'] = temp_dict['manage_etc_hosts']
            with open(self.vnfm_dir+'/userdata', 'w') as ud_file:
                yaml.safe_dump(data, ud_file)
        else:
          with open(self.vnfm_dir+'/userdata', 'w') as ud_file:
              yaml.safe_dump(temp_dict, ud_file)
        return self.vnfm_dir+'/userdata'


    def set_default_userdata_loadbalancer(self, vm_details):
        nics = vm_details['nics']
        cidr = ''
        for network in nics:
            if network['net-id'] != self.networks['mgmt-if']:
                 subnet_id = network['subnet-id']
                 cidr = self.neutronclient.show_subnet(subnet_id)['subnet']['cidr']
                 break
        if  cidr != ''  and 'userdata' in vm_details.keys():
            with open(vm_details['userdata'], 'r') as f:
                data = yaml.safe_load(f)
            ip  = cidr.split('/')[0]
            ip = ip[0:-1]+'1'
            data['runcmd'].insert(1,"sudo ip route del default")
            data['runcmd'].insert(2,"sudo ip route add default via "+ ip +" dev eth1")
            with open(vm_details['userdata'], 'w') as userdata_file:
                yaml.safe_dump(data, userdata_file)


    def _populate_instances_id(self, vnfd_name, vdu_name):
        self.instances[vnfd_name+':'+vdu_name] = []
        for instance in self.vnfds[vnfd_name]['vdus'][vdu_name]['instances']:
            self.instances[vnfd_name+':'+vdu_name].append(instance.id)


    def _generate_vnfm_conf(self):
        vnfm_dict = {}
        vnfm_dict['service'] = {}
        current_vnfs = [vdu for vdu in self.deployed_vdus if vdu not in self.conf_generated]
        for vnf in current_vnfs:
            if not self.is_manager_invoked:
                vnfm_dict['service']['id'] = self.service_name
                vnfm_dict['service']['fg'] = self.nsd_template['postconfigure']['forwarding_graphs']
            vnfd_name, vdu_name = vnf.split(':')[0],vnf.split(':')[1]
            if vnfd_name  not in vnfm_dict['service'].keys(): 
                vnfm_dict['service'][vnfd_name] = list()
            vdu_dict = {}
            vdu_dict['name'] = vdu_name
            vdu = self.vnfds[vnfd_name]['vdus'][vdu_name]
            for property in vdu:
                if property not in ['preconfigure', 'instances']:
                    if property == 'postconfigure':
                        vdu_dict.update(vdu['postconfigure'])
                    else:
                        vdu_dict[property] = vdu[property]
            vnfm_dict['service'][vnfd_name].append(vdu_dict)
            self.conf_generated.append(vnf)

        return vnfm_dict


    def _boot_vdu(self, context, vnfd, **vm_details):
        instance = self.novaclient.server_create(**vm_details)
        if vm_details['num_instances'] == 1:
            instance = self.novaclient.get_server(instance.id)
            self.update_vdu_instance_details(context, instance.id, self.nsd_template['vdus'][vnfd]['id'])
            while instance.status != 'ACTIVE' or \
                   all(not instance.networks[iface] for iface in instance.networks.keys()):
                time.sleep(3)
                instance = self.novaclient.get_server(instance.id)
                if instance.status == 'ERROR':
                    self.update_nsd_status(context, self.nsd_id, 'ERROR')
                    raise exceptions.InstanceException()
        else:
            instances_list = instance
            instance = list()
            instances_active = 0
            temp_instance = None
            for temp_instance in instances_list:
                self.update_vdu_instance_details(context, temp_instance.id,
                                self.nsd_template['vdus'][vnfd]['id'])
            while instances_active != vm_details['num_instances'] or len(instances_list) > 0:
                for inst in instances_list:
                    temp_instance = self.novaclient.get_server(inst.id)
                    if temp_instance.status == 'ACTIVE':
                        instances_active += 1
                        instances_list.remove(inst)
                        instance.append(inst)
                    elif temp_instance.status == 'ERROR':
                        self.update_nsd_status(self.nsd_id, 'ERROR')
                        raise exceptions.InstanceException()
                    else:
                        time.sleep(3)

        return instance
 

    def _set_instance_ip(self, vnfd_name, vdu_name):
        instances = self.vnfds[vnfd_name]['vdus'][vdu_name]['instances']
        ninterfaces = self.vnfds[vnfd_name]['vdus'][vdu_name]['vm_details']['network_interfaces']
        for interface in ninterfaces:
            subnet =self.neutronclient.show_subnet(ninterfaces[interface]['subnet-id'])
            cidr = subnet['subnet']['cidr']
            ninterfaces[interface]['ips'] = self._get_ips(instances, cidr)

        
    def _get_ips(self, instances, cidr):
        ip_list = {}
        for instance in instances:
            instance_name = instance.name
            networks = instance.addresses
            for network in networks.keys():
                for i in range(len(networks[network])):
                    ip = networks[network][i]['addr']
                    if IPAddress(ip) in IPNetwork(cidr):
                       ip_list[instance_name]= ip
        return ip_list 


    def _set_mgmt_ip(self,vnfd_name, vdu_name):
        instances = self.vnfds[vnfd_name]['vdus'][vdu_name]['instances']
        self.vnfds[vnfd_name]['vdus'][vdu_name]['mgmt-ip'] = {}
        mgmt_cidr = self.nsd_template['mgmt-cidr']
        for instance in instances:
            networks = instance.addresses
            for network in networks.keys():
                for subnet in networks[network]:
                    ip = subnet['addr']
                    if IPAddress(ip) in IPNetwork(mgmt_cidr):
                        self.vnfds[vnfd_name]['vdus'][vdu_name]['mgmt-ip'][instance.name] = ip


    def _copy_vnfmanager(self):
        src = self.conf.vnf.vnfmanager
        dest = '/tmp/vnfmanager'
        try:
            dir_util.copy_tree(src, dest)
            return dest
        except OSError as exc:
            raise


    def get_service(self, context, service, **kwargs):
        service = self.get_service_model(context, service, fields=None)
        return service

    def get_services(self,context, **kwargs):
        service=self.get_all_services(context, **kwargs)
        return service


    def _delete_vnf_file(self, vnf_id):
        for root,dirs,files in os.walk(self.conf.state_path+"/"):
            for name in files:
                if name == vnf_id:
                    os.remove(os.path.join(root,name))

    def _vnf_conf_file(self, vnf_details, vnfmanager_uuid):
        """Write the required data into buffer
           and then into required file."""
        buf = six.StringIO()
        for vnf_detail in vnf_details:
            vnf_data = vnf_detail.__dict__
            del vnf_data['_sa_instance_state']
            del vnf_data['id']
            if 'service_type' in vnf_data.keys():
                buf.write('[%s]\n' % vnf_data['service_type'])
                del vnf_data['service_type']
            for key in vnf_data.keys():
                    buf.write('%s:%s\n' % (key, vnf_data[key]))
            buf.write('\n')

        vnf_file = self._get_conf_file_name(vnfmanager_uuid)
        utils._replace_file(vnf_file, buf.getvalue())
        return vnf_file
  

    def _get_conf_file_name(self, vnfmanager_uuid, ensure_conf_dir=True):
        """Returns the file name for a given kind of config file."""

        confs_dir = os.path.abspath(os.path.normpath(self.conf.state_path))
        if ensure_conf_dir:
            if not os.path.isdir(confs_dir):
                os.makedirs(confs_dir, 0o755)
        return os.path.join(confs_dir, vnfmanager_uuid)

 
    def _make_tar(self, vnfmanager_path):
        tar = tarfile.open(vnfmanager_path+'.tar.gz', 'w:gz')
        tar.add(vnfmanager_path)
        tar.close() 
        return vnfmanager_path+'.tar.gz'


    def _invoke_vnf_manager(self, context):
        """Invokes VNF manager using ansible(if multihost)"""
        vnfm_conf_dict = self._generate_vnfm_conf()
        with open(self.vnfm_dir+'/'+self.vnfmanager_uuid+'.yaml', 'w') as f:
            yaml.safe_dump(vnfm_conf_dict, f)
        vnfm_conf = self.vnfm_dir+'/'+self.vnfmanager_uuid+'.yaml'
        vnfsvc_conf = cfg.CONF.config_file[0]
        
        vnfmanager_path = self._copy_vnfmanager()
        #self.copy_drivers()
        #vnfmanager_path = self._make_tar(vnfmanager_path)

        ovs_path = self._create_ovs_script(self.nsd_template['networks']['mgmt-if']['id'])
        vnfm_host =  self.novaclient.check_host(cfg.CONF.vnf.compute_hostname)
        if cfg.CONF.vnf.vnfmconf == "local":
            confcmd  = 'vnf-manager --config-file /etc/vnfsvc/vnfsvc.conf --vnfm-conf-dir '+self.vnfm_dir+'/ --log-file '+self.vnfm_dir+'/vnfm.log --uuid '+self.vnfmanager_uuid
            ovscmd =  'sudo sh '+self.vnfm_dir+'/ovs.sh'
            proc = subprocess.Popen(ovscmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            proc2 = subprocess.Popen(confcmd, shell=True, stdin=subprocess.PIPE, stdout =subprocess.PIPE, stderr=subprocess.PIPE)
        #TODO: (tcs) Still need to enhance (currently supported only for single host)
        elif cfg.CONF.vnf.vnfmconf == "ansible":
            with open(self.vnfm_dir+'/hosts', 'w') as hosts_file:
                 hosts_file.write("[server]\n%s\n"%(vnfm_host.host_ip))
            vnfm_home_dir =  cfg.CONF.vnf.vnfm_home_dir+self.vnfmanager_uuid
         
            ansible_dict=[{'tasks': [
                            {'ignore_errors': True, 'shell': 'mkdir -p '+vnfm_home_dir},
                            {'copy': 'src='+vnfsvc_conf+' dest='+vnfm_home_dir+'/vnfsvc.conf', 'sudo': 'yes'},
                            {'sudo': 'yes', 'copy': 'src='+vnfmanager_path+' dest='+vnfm_home_dir, 'name': 'move vnf manager'},
                            {'ignore_errors': True, 'shell': "pip freeze | grep 'vnf-manager'", 'name': 'check whether the vnf manager is installed or not', 'register': 'result'},
                            {'shell': 'cd '+vnfm_home_dir+'/vnfmanager && git init', 'sudo': 'yes'},
                            {'shell': 'cd '+vnfm_home_dir+'/vnfmanager && sudo python setup.py install', 'when': 'result.rc!=0', 'name': 'install manager'},
                            {'copy': 'src='+vnfm_conf+' dest='+vnfm_home_dir+'/'+self.vnfmanager_uuid+'.yaml', 'sudo': 'yes'},
                            {'async': 1000000, 'poll': 0,
                                 'command': 'vnf-manager --config-file '+vnfm_home_dir+'/vnfsvc.conf --vnfm-conf-dir '+vnfm_home_dir+'/ --log-file '+vnfm_home_dir+'/vnfm.log --uuid %s'
                                 % self.vnfmanager_uuid, 'name': 'run manager'},
                            {'copy': 'src='+ovs_path+' dest='+vnfm_home_dir+'/ovs.sh'},
                            {'ignore_errors': True, 'shell': 'sh '+vnfm_home_dir+'/ovs.sh', 'register': 'result1'},
                            {'debug': 'var=result1.stdout_lines'},
                        ], 'hosts': 'server', 'remote_user': cfg.CONF.vnf.compute_user}]
         
            with open(self.vnfm_dir+'/vnfmanager-playbook.yaml', 'w') as yaml_file:
                yaml_file.write( yaml.dump(ansible_dict, default_flow_style=False))
            LOG.debug(_('----- Launching VNF Manager -----'))
            child = pexpect.spawn('ansible-playbook '+self.vnfm_dir+'/vnfmanager-playbook.yaml -i '+self.vnfm_dir+'/hosts --ask-pass', timeout=None)
            child.expect('SSH password:')
            child.sendline(cfg.CONF.vnf.ssh_pwd)
            result =  child.readlines()
        self.agent_mapping[self.vnfmanager_uuid] = VNFManagerAgentApi(
                                                 topics.get_topic_for_mgr(self.vnfmanager_uuid),
                                                 cfg.CONF.host, self)
        self.is_manager_invoked = True
        self._resolve_dependency(context)

    def _create_ovs_script(self, mgmt_id):
        int_br = ovs_lib.OVSBridge(cfg.CONF.vnf.ovs_bridge, "sudo "+cfg.CONF.vnf.neutron_rootwrap+" "+cfg.CONF.vnf.neutron_rootwrapconf)
        port_tags_data = int_br.get_port_tag_dict()
        nc = self.neutronclient
        ports = nc.get_ports()
        vlan_tag = ''
        for port in ports:
            if port['network_id'] == mgmt_id and port['device_owner'] == "network:dhcp":
                port_id = port['id']
                tap_name  = "tap"+port_id[:11]
                vlan_tag = port_tags_data[tap_name]
        v_port = nc.create_port({'port':{'network_id': mgmt_id}})
        p_id = v_port['port']['id']
        lines_dict = []
        lines_dict.append('#!/bin/sh\n')
        lines_dict.append('sudo ovs-vsctl add-port br-int vtap-%s -- set interface vtap-%s type=internal -- set port vtap-%s tag=%s\n'
                %(str(p_id)[:8],str(p_id)[:8],str(p_id)[:8],vlan_tag))
        lines_dict.append('sudo ifconfig vtap-%s %s up\n'
                %(str(p_id)[:8],str(v_port['port']['fixed_ips'][0]['ip_address'])))
        with open(self.vnfm_dir+'/ovs.sh', 'w') as f:
            f.writelines(lines_dict)
        return self.vnfm_dir+'/ovs.sh'


    def build_acknowledge_list(self, vnfd_name, vdu_name, instance, status):
        vdu = vnfd_name+':'+vdu_name
        if status == 'ERROR':
            self.update_nsd_status(self.nsd_id, 'ERROR')
            raise exceptions.ConfigurationError
        else:
            #check whether the key exists or not
            if self.acknowledge_list.get(vdu, None):
                self.acknowledge_list[vdu].append(instance)
            else:
                self.acknowledge_list[vdu] = [instance]

            #check whether all the instances of a specific VDU are acknowledged or not
            vdu_instances = len(self.vnfds[vnfd_name]['vdus'][vdu_name]['instances'])
            current_instances = len(self.acknowledge_list[vdu])
            if vdu_instances == current_instances:
            	self.created.append(vdu)

 
class VNFManagerAgentApi(v_rpc.RpcProxy):
    """Plugin side of plugin to agent RPC API."""

    API_VERSION = '1.0'

    def __init__(self, topic, host, plugin):
        super(VNFManagerAgentApi, self).__init__(topic, self.API_VERSION)
        self.host = host
        self.plugin = plugin


    def configure_vdus(self, context, conf):
        return self.cast(
            context,
            self.make_msg('configure_vdus', conf=conf),
        )


class VNFManagerCallbacks(v_rpc.RpcCallback):
    RPC_API_VERSION = '1.0'

    def __init__(self, plugin):
        super(VNFManagerCallbacks, self).__init__()
        self.plugin = plugin

    def send_ack(self, context, vnfd, vdu, instance, status):
        if status == 'COMPLETE':
            self.plugin.build_acknowledge_list(vnfd, vdu, instance, status)
            LOG.debug(_('ACK received from VNF Manager: Configuration complete for VNF %s'), instance)
        else:
            self.plugin.build_acknowledge_list(vnfd, vdu, instance, status)
            LOG.debug(_('ACK received from VNF Manager: Confguration failed for VNF %s'), instance)
