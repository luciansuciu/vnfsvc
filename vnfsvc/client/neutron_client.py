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



from neutronclient.v2_0 import client as client_v2
from oslo.config import cfg
from vnfsvc.openstack.common import jsonutils
from vnfsvc import constants
from vnfsvc.openstack.common.gettextutils import _

SERVICE_OPTS = [
    cfg.StrOpt('project_id', default='',
               help=_('project id used '
                      'by nova driver of service vm extension')),
    cfg.StrOpt('auth_url', default='http://0.0.0.0:5000/v2.0',
               help=_('auth URL used by nova driver of service vm extension')),
    cfg.StrOpt('user_name', default='',
               help=_('user name used '
                      'by nova driver of service vm extension')),
    cfg.StrOpt('api_key', default='',
               help=_('api-key used by nova driver of service vm extension')),
    cfg.StrOpt('tenant_name', default='',
               help=_('tenant name used by nova driver of service vm extension')),

    cfg.StrOpt('ca-file',
               help=_('Optional CA cert file for nova driver to use in SSL'
                      ' connections ')),
    cfg.BoolOpt('insecure', default=False,
                help=_("If set then the server's certificate will not "
                       "be verified by nova driver")),
]
#CONF = cfg.CONF
cfg.CONF.register_opts(SERVICE_OPTS, group='vnf_credentials')




class Client(object):
    """A client which gets information via python-neutronclient."""

    def __init__(self):
        conf = cfg.CONF.vnf_credentials
        params = {
            'username': conf.user_name,
            'password': conf.api_key,
            'auth_url': conf.auth_url,
        }

        if conf.project_id:
            params['tenant_id'] = conf.project_id
        else:
            params['tenant_name'] = conf.tenant_name

        self._client = client_v2.Client(**params)

    def get_networks(self):
        """Returns all networks."""
        resp = self._client.list_networks()
        return resp.get('networks')

    def get_ports(self):
        resp = self._client.list_ports()
        return resp.get('ports')

    def create_device_template(self, device_template):
        return self._client.create_device_template(body=device_template)

    def create_device(self, device):
        return self._client.create_device(body=device)

    def mgmt_address(self, device, instance_ips):
        for sc_entry in device['service_context']:
            if sc_entry['role'] == constants.ROLE_MGMT :
                network_id = sc_entry['network_id']
        ports = self.get_ports()
        for mgmt_port in ports:
            if mgmt_port['network_id'] == network_id and mgmt_port['fixed_ips'][0]['ip_address'] in instance_ips:
                port_id = mgmt_port['id']
        port = self._client.show_port(port_id).get('port')
        if not port:
            return
        mgmt_address = port['fixed_ips'][0]
        mgmt_address['network_id'] = port['network_id']
        mgmt_address['port_id'] = port['id']
        mgmt_address['mac_address'] = port['mac_address']

        return jsonutils.dumps(mgmt_address)

    def get_device(self, device_id):
        resp = self._client.show_device(device_id)
        return resp.get('device')

    def get_device_template(self, device_template_id):
        resp = self._client.show_device_template(device_template_id)
        return resp.get('device_template')

    def get_device_templates(self):
        resp = self._client.list_device_templates()
        return resp.get('device_templates')

    def delete_device_template(self, device_template_id):
        self._client.delete_device_template(device_template_id)

    def delete_device(self, device_id):
        self._client.delete_device(device_id)

    def get_router(self, router):
        return self._client.list_routers(name=router).get('routers')

    def add_interface_router(self, router_id, subnet_id):
        return self._client.add_interface_router(router_id, {'subnet_id':subnet_id})