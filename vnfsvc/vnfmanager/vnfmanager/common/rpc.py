# Copyright (c) 2012 OpenStack Foundation.
# Copyright (c) 2014 Red Hat, Inc.
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

from oslo.config import cfg
from oslo import messaging
from oslo.messaging.rpc import dispatcher as rpc_dispatcher
from oslo.messaging import serializer as om_serializer
from oslo.messaging import server as msg_server

from vnfmanager.common import log
from vnfmanager import context
from vnfmanager.openstack.common import log as logging
from vnfmanager.openstack.common import service


LOG = logging.getLogger(__name__)


TRANSPORT = None
NOTIFIER = None

EXTRA_EXMODS = []


TRANSPORT_ALIASES = {
    'vnfmanager.openstack.common.rpc.impl_fake': 'fake',
    'vnfmanager.openstack.common.rpc.impl_qpid': 'qpid',
    'vnfmanager.openstack.common.rpc.impl_kombu': 'rabbit',
    'vnfmanager.openstack.common.rpc.impl_zmq': 'zmq',
    'vnfmanager.rpc.impl_fake': 'fake',
    'vnfmanager.rpc.impl_qpid': 'qpid',
    'vnfmanager.rpc.impl_kombu': 'rabbit',
    'vnfmanager.rpc.impl_zmq': 'zmq',
}


def init(conf):
    global TRANSPORT, NOTIFIER
    exmods = get_allowed_exmods()
    TRANSPORT = messaging.get_transport(conf,
                                        allowed_remote_exmods=exmods,
                                        aliases=TRANSPORT_ALIASES)
    serializer = RequestContextSerializer()
    NOTIFIER = messaging.Notifier(TRANSPORT, serializer=serializer)


def cleanup():
    global TRANSPORT, NOTIFIER
    assert TRANSPORT is not None
    assert NOTIFIER is not None
    TRANSPORT.cleanup()
    TRANSPORT = NOTIFIER = None


def add_extra_exmods(*args):
    EXTRA_EXMODS.extend(args)


def clear_extra_exmods():
    del EXTRA_EXMODS[:]

def get_allowed_exmods():
    return EXTRA_EXMODS


def get_client(target, version_cap=None, serializer=None):
    assert TRANSPORT is not None
    serializer = RequestContextSerializer(serializer)
    return messaging.RPCClient(TRANSPORT,
                               target,
                               version_cap=version_cap,
                               serializer=serializer)


def get_server(target, endpoints, serializer=None):
    assert TRANSPORT is not None
    serializer = RequestContextSerializer(serializer)
    dispatcher = RPCDispatcher(target, endpoints, serializer)
    return msg_server.MessageHandlingServer(TRANSPORT, dispatcher, 'eventlet')


def get_notifier(service=None, host=None, publisher_id=None):
    assert NOTIFIER is not None
    if not publisher_id:
        publisher_id = "%s.%s" % (service, host or cfg.CONF.host)
    return NOTIFIER.prepare(publisher_id=publisher_id)


class RPCDispatcher(rpc_dispatcher.RPCDispatcher):
    def __call__(self, incoming):
        sanitize_key_list = ('auth_token', )
        sanitized_ctxt = dict((k, '***' if k in sanitize_key_list else v)
                              for (k, v) in incoming.ctxt.items())
        LOG.debug('Incoming RPC: ctxt:%s message:%s', sanitized_ctxt,
                  incoming.message)
        return super(RPCDispatcher, self).__call__(incoming)


class RequestContextSerializer(om_serializer.Serializer):
    """This serializer is used to convert RPC common context into
    Vnfmanager Context.
    """
    def __init__(self, base=None):
        super(RequestContextSerializer, self).__init__()
        self._base = base

    def serialize_entity(self, ctxt, entity):
        if not self._base:
            return entity
        return self._base.serialize_entity(ctxt, entity)

    def deserialize_entity(self, ctxt, entity):
        if not self._base:
            return entity
        return self._base.deserialize_entity(ctxt, entity)

    def serialize_context(self, ctxt):
        return ctxt.to_dict()

    def deserialize_context(self, ctxt):
        rpc_ctxt_dict = ctxt.copy()
        user_id = rpc_ctxt_dict.pop('user_id', None)
        if not user_id:
            user_id = rpc_ctxt_dict.pop('user', None)
        tenant_id = rpc_ctxt_dict.pop('tenant_id', None)
        if not tenant_id:
            tenant_id = rpc_ctxt_dict.pop('project_id', None)
        return context.Context(user_id, tenant_id,
                               load_admin_roles=False, **rpc_ctxt_dict)


class RpcProxy(object):
    '''
    This class is created to facilitate migration from oslo-incubator
    RPC layer implementation to oslo.messaging and is intended to
    emulate RpcProxy class behaviour using oslo.messaging API once the
    migration is applied.
    '''
    RPC_API_NAMESPACE = None

    def __init__(self, topic, default_version, version_cap=None):
        super(RpcProxy, self).__init__()
        self.topic = topic
        target = messaging.Target(topic=topic, version=default_version)
        self._client = get_client(target, version_cap=version_cap)

    def make_msg(self, method, **kwargs):
        return {'method': method,
                'namespace': self.RPC_API_NAMESPACE,
                'args': kwargs}

    @log.log
    def call(self, context, msg, **kwargs):
        return self.__call_rpc_method(
            context, msg, rpc_method='call', **kwargs)

    @log.log
    def cast(self, context, msg, **kwargs):
        self.__call_rpc_method(context, msg, rpc_method='cast', **kwargs)

    @log.log
    def fanout_cast(self, context, msg, **kwargs):
        kwargs['fanout'] = True
        self.__call_rpc_method(context, msg, rpc_method='cast', **kwargs)

    def __call_rpc_method(self, context, msg, **kwargs):
        options = dict(
            ((opt, kwargs[opt])
             for opt in ('fanout', 'timeout', 'topic', 'version')
             if kwargs.get(opt))
        )
        if msg['namespace']:
            options['namespace'] = msg['namespace']

        if options:
            callee = self._client.prepare(**options)
        else:
            callee = self._client

        func = getattr(callee, kwargs['rpc_method'])
        return func(context, msg['method'], **msg['args'])


class RpcCallback(object):
    '''
    This class is created to facilitate migration from oslo-incubator
    RPC layer implementation to oslo.messaging and is intended to set
    callback version using oslo.messaging API once the migration is
    applied.
    '''
    RPC_API_VERSION = '1.0'

    def __init__(self):
        super(RpcCallback, self).__init__()
        self.target = messaging.Target(version=self.RPC_API_VERSION)


class Service(service.Service):
    """Service object for binaries running on hosts.

    A service enables rpc by listening to queues based on topic and host.
    """
    def __init__(self, host, topic, manager=None, serializer=None):
        super(Service, self).__init__()
        self.host = host
        self.topic = topic
        self.serializer = serializer
        if manager is None:
            self.manager = self
        else:
            self.manager = manager

    def start(self):
        super(Service, self).start()

        self.conn = create_connection(new=True)
        LOG.debug("Creating Consumer connection for Service %s" %
                  self.topic)

        endpoints = [self.manager]

        # Share this same connection for these Consumers
        self.conn.create_consumer(self.topic, endpoints, fanout=False)

        node_topic = '%s.%s' % (self.topic, self.host)
        self.conn.create_consumer(node_topic, endpoints, fanout=False)

        self.conn.create_consumer(self.topic, endpoints, fanout=True)

        # Hook to allow the manager to do other initializations after
        # the rpc connection is created.
        if callable(getattr(self.manager, 'initialize_service_hook', None)):
            self.manager.initialize_service_hook(self)

        # Consume from all consumers in threads
        self.conn.consume_in_threads()

    def stop(self):
        # Try to shut the connection down, but if we get any sort of
        # errors, go ahead and ignore them.. as we're shutting down anyway
        try:
            self.conn.close()
        except Exception:
            pass
        super(Service, self).stop()


class Connection(object):

    def __init__(self):
        super(Connection, self).__init__()
        self.servers = []

    def create_consumer(self, topic, endpoints, fanout=False):
        target = messaging.Target(
            topic=topic, server=cfg.CONF.host, fanout=fanout)
        server = get_server(target, endpoints)
        self.servers.append(server)

    def consume_in_threads(self):
        for server in self.servers:
            server.start()
        return self.servers


# functions
def create_connection(new=True):
    return Connection()


# exceptions
RPCException = messaging.MessagingException
RemoteError = messaging.RemoteError
MessagingTimeout = messaging.MessagingTimeout
