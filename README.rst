=======
VNFSVC
=======

OpenVNFManager enables NFV service orchestration on openstack platform

* It has 2 key components::

    $ vnfsvc 
    $ vnfManager

vnfsvc
-------

* Runs as a service [ similar to openstack neutron etc ] on the controller node
  It implements server side for vnfsvcclient and HEAT

  To install::

    $ git clone https://github.com/TCS-TelcoCloud/OpenVNFManager.git
    $ python install setup.py

* Check::

    $ api-paste.ini,  rootwrap.conf,  rootwrap.d,  templates.json,  vnfsvc.conf exists in /etc/vnfsvc/ [ on RedHat Linux/Centos7/Fedora ]
    $ passwords and urls of the openstack services in /etc/vnfsvc/vnfsvc.conf

* Create keystone endpoint (please refer vnfsvc-kystn-regs in vnfsvc_examples folder)
  Execute the following commands::

    $ create database vnfsvc; (MYSQL)
    $ grant all privileges on vnfsvc.* to 'vnfsvc'@'localhost' identified by <database password>; (MYSQL)
    $ grant all privileges on vnfsvc.* to 'vnfsvc'@'%' identified by <database password>; (MYSQL)
    $ vnfsvc-db-manage --config-file /etc/vnfsvc/vnfsvc.conf upgrade head
    $ mkdir /var/log/vnfsvc

* Run with the following command to start the server::

    $ python /usr/bin/vnfsvc-server  --config-file /etc/vnfsvc/vnfsvc.conf --log-file /var/log/vnfsvc/server.log 

vnfManager
-----------

Interfaces with VNFs and vnfsvc for configuration and lifecycle management of virtual network functions
In the current setup init is the only supported lifecycle event

* Sample descriptors and howTo are provided in vnfsvc_examples folder. It has::

    $ HA Proxy image 
    $ Webserver image
    $ NSD 
    $ VNFD
    $ HEAT template
    $ README for running the installation

* After installing vnfsvc, python-vnfsvcclient and HEAT updates, run the setup as detailed in vnfsvc_examples

