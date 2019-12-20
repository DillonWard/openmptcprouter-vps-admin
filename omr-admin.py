#!/usr/bin/env python3
# Copyright (C) 2018-2019 Ycarus (Yannick Chabanois) <ycarus@zugaina.org>
#
# This is free software, licensed under the GNU General Public License v3.0.
# See /LICENSE for more information.
#

import json
import base64
import uuid
import configparser
import subprocess
import os
import socket
import re
import hashlib
import time
import uvicorn
import jwt
from jwt import PyJWTError
from pprint import pprint
from datetime import datetime,timedelta
from tempfile import mkstemp
from typing import List
from shutil import move
from pprint import pprint
from netjsonconfig import OpenWrt
from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm, SecurityScopes
from passlib.context import CryptContext
from pydantic import BaseModel, ValidationError
from starlette.status import HTTP_401_UNAUTHORIZED
#from flask import Flask, jsonify, request, session
#from flask_jwt_simple import (
#    JWTManager, jwt_required, create_jwt, get_jwt_identity
#)


import logging
log = logging.getLogger('api')
#log.setLevel(logging.ERROR)
log.setLevel(logging.DEBUG)

# Generate a random secret key
SECRET_KEY = uuid.uuid4().hex
JWT_SECRET_KEY = uuid.uuid4().hex
PERMANENT_SESSION_LIFETIME = timedelta(hours=24)
ACCESS_TOKEN_EXPIRE_MINUTES = 1440
ALGORITHM = "HS256"

# Get main net interface
file = open('/etc/shorewall/params.net', "r")
read = file.read()
iface = None
for line in read.splitlines():
    if 'NET_IFACE=' in line:
        iface=line.split('=',1)[1]

# Get interface rx/tx
def get_bytes(t, iface='eth0'):
    with open('/sys/class/net/' + iface + '/statistics/' + t + '_bytes', 'r') as f:
        data = f.read();
    return int(data)

def ordered(obj):
    if isinstance(obj, dict):
        return sorted((k, ordered(v)) for k, v in obj.items())
    if isinstance(obj, list):
        return sorted(ordered(x) for x in obj)
    else:
        return obj

def file_as_bytes(file):
    with file:
        return file.read()

def shorewall_add_port(port,proto,name,fwtype='ACCEPT'):
    initial_md5 = hashlib.md5(file_as_bytes(open('/etc/shorewall/rules', 'rb'))).hexdigest()
    fd, tmpfile = mkstemp()
    with open('/etc/shorewall/rules','r') as f, open(tmpfile,'a+') as n:
        for line in f:
            if fwtype == 'ACCEPT' and not port + '	# OMR open ' + name + ' port ' + proto in line:
                n.write(line)
            elif fwtype == 'DNAT' and not port + '	# OMR redirect ' + name + ' port ' + proto in line:
                n.write(line)
        if fwtype == 'ACCEPT':
            n.write('ACCEPT		net		$FW		' + proto + '	' + port + '	# OMR open ' + name + ' port ' + proto + "\n")
        elif fwtype == 'DNAT':
            n.write('DNAT		net		vpn:$OMR_ADDR	' + proto + '	' + port + '	# OMR redirect ' + name + ' port ' + proto + "\n")
    os.close(fd)
    move(tmpfile,'/etc/shorewall/rules')
    final_md5 = hashlib.md5(file_as_bytes(open('/etc/shorewall/rules', 'rb'))).hexdigest()
    if not initial_md5 == final_md5:
        os.system("systemctl -q reload shorewall")

def shorewall_del_port(port,proto,name,fwtype='ACCEPT'):
    initial_md5 = hashlib.md5(file_as_bytes(open('/etc/shorewall/rules', 'rb'))).hexdigest()
    fd, tmpfile = mkstemp()
    with open('/etc/shorewall/rules','r') as f, open(tmpfile,'a+') as n:
        for line in f:
            if fwtype == 'ACCEPT' and not port + '	# OMR open ' + name + ' port ' + proto in line:
                n.write(line)
            elif fwtype == 'DNAT' and not port + '	# OMR redirect ' + name + ' port ' + proto in line:
                n.write(line)
    os.close(fd)
    move(tmpfile,'/etc/shorewall/rules')
    final_md5 = hashlib.md5(file_as_bytes(open('/etc/shorewall/rules', 'rb'))).hexdigest()
    if not initial_md5 == final_md5:
        os.system("systemctl -q reload shorewall")

def set_lastchange():
    with open('/etc/openmptcprouter-vps-admin/omr-admin-config.json') as f:
        content = f.read()
    content = re.sub(",\s*}","}",content)
    try:
        data = json.loads(content)
    except ValueError as e:
        return jsonify({'error': 'Config file not readable','route': 'lastchange'}), 200
    data["lastchange"] = time.time()
    with open('/etc/openmptcprouter-vps-admin/omr-admin-config.json','w') as outfile:
        json.dump(data,outfile,indent=4)

with open('/etc/openmptcprouter-vps-admin/omr-admin-config.json') as f:
    omr_config_data = json.load(f)

fake_users_db = omr_config_data['users'][0]

def verify_password(plain_password, user_password):
    #return pwd_context.verify(plain_password, user_password)
    if plain_password == user_password:
        log.debug("password true")
        return True
    return False

def get_password_hash(password):
    #return pwd_context.hash(password)
    return password


def get_user(db, username: str):
    if username in db:
        user_dict = db[username]
        return UserInDB(**user_dict)

def authenticate_user(fake_db, username: str, password: str):
    user = get_user(fake_db, username)
    if not user:
        log.debug("user doesn't exist")
        return False
    if not verify_password(password, user.user_password):
        log.debug("wrong password")
        return False
    return user

class Token(BaseModel):
    access_token: str
    token_type: str


class TokenData(BaseModel):
    username: str = None

class User(BaseModel):
    username: str
#    email: str = None
    shadowsocks_port: int = None
    disabled: bool = None


class UserInDB(User):
    user_password: str

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/token")
app = FastAPI()


pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl="/token",
    scopes={"me": "Read information about the current user.", "items": "Read items."},
)

def create_access_token(*, data: dict, expires_delta: timedelta = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

async def get_current_user(token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(
        status_code=HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
        token_data = TokenData(username=username)
    except PyJWTError:
        raise credentials_exception
    user = get_user(fake_users_db, username=token_data.username)
    if user is None:
        raise credentials_exception
    return user

# Provide a method to create access tokens. The create_jwt()
# function is used to actually generate the token
@app.post('/token', response_model=Token)
async def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends()):
    user = authenticate_user(fake_users_db, form_data.username, form_data.password)
    if not user:
        raise HTTPException(status_code=400, detail="Incorrect username or password")

    # Identity can be any data that is json serializable
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user.username}, expires_delta=access_token_expires
    )
    return {"access_token": access_token, "token_type": "bearer"}


# Get VPS status
@app.get('/status')
def status(current_user: User = Depends(get_current_user)):
    vps_loadavg = os.popen("cat /proc/loadavg | awk '{print $1\" \"$2\" \"$3}'").read().rstrip()
    vps_uptime = os.popen("cat /proc/uptime | awk '{print $1}'").read().rstrip()
    vps_hostname = socket.gethostname()
    vps_current_time = time.time()
    mptcp_enabled = os.popen('sysctl -n net.mptcp.mptcp_enabled').read().rstrip()

    if iface:
        return {'vps': {'time': vps_current_time,'loadavg': vps_loadavg,'uptime': vps_uptime,'mptcp': mptcp_enabled,'hostname': vps_hostname}, 'network': {'tx': get_bytes('tx',iface),'rx': get_bytes('rx',iface)}}
    else:
        return {'error': 'No iface defined','route': 'status'}

# Get VPS config
@app.get('/config')
def config(current_user: User = Depends(get_current_user)):
    with open('/etc/openmptcprouter-vps-admin/omr-admin-config.json') as f:
        try:
            omr_config_data = json.load(f)
        except ValueError as e:
            omr_config_data = {}
    with open('/etc/shadowsocks-libev/manager.json') as f:
        content = f.read()
    content = re.sub(",\s*}","}",content)
    try:
        data = json.loads(content)
    except ValueError as e:
        data = {'key': '', 'server_port': 65101, 'method': 'chacha20'}
    #shadowsocks_port = data["server_port"]
    shadowsocks_port = current_user.shadowsocks_port
    shadowsocks_key = data["port_key"][str(shadowsocks_port)]
    shadowsocks_method = data["method"]
    if 'fast_open' in data:
        shadowsocks_fast_open = data["fast_open"]
    else:
        shadowsocks_fast_open = False
    if 'reuse_port' in data:
        shadowsocks_reuse_port = data["reuse_port"]
    else:
        shadowsocks_reuse_port = False
    if 'no_delay' in data:
        shadowsocks_no_delay = data["no_delay"]
    else:
        shadowsocks_no_delay = False
    if 'mptcp' in data:
        shadowsocks_mptcp = data["mptcp"]
    else:
        shadowsocks_mptcp = False
    if 'ebpf' in data:
        shadowsocks_ebpf = data["ebpf"]
    else:
        shadowsocks_ebpf = False
    if "plugin" in data:
        shadowsocks_obfs = True
        if 'v2ray' in data["plugin"]:
            shadowsocks_obfs_plugin = 'v2ray'
        else:
            shadowsocks_obfs_plugin = 'obfs'
        if 'tls' in data["plugin_opts"]:
            shadowsocks_obfs_type = 'tls'
        else:
            shadowsocks_obfs_type = 'http'
    else:
        shadowsocks_obfs = False
        shadowsocks_obfs_plugin = ''
        shadowsocks_obfs_type = ''
    if os.path.isfile('/etc/glorytun-tcp/tun0.key'):
        glorytun_key = open('/etc/glorytun-tcp/tun0.key').readline().rstrip()
    else:
        glorytun_key = ''
    glorytun_port = '65001'
    glorytun_chacha = False
    if os.path.isfile('/etc/glorytun-tcp/tun0'):
        with open('/etc/glorytun-tcp/tun0',"r") as glorytun_file:
            for line in glorytun_file:
                if 'PORT=' in line:
                    glorytun_port = line.replace(line[:5], '').rstrip()
                if 'chacha' in line:
                    glorytun_chacha = True
    if 'glorytun_tcp_type' in omr_config_data:
        if omr_config_data['glorytun_tcp_type'] == 'static':
            glorytun_tcp_host_ip = '10.255.255.1'
            glorytun_tcp_client_ip = '10.255.255.2'
        else:
            glorytun_tcp_host_ip = 'dhcp'
            glorytun_tcp_client_ip = 'dhcp'
    else:
        glorytun_tcp_host_ip = '10.255.255.1'
        glorytun_tcp_client_ip = '10.255.255.2'
    if 'glorytun_udp_type' in omr_config_data:
        if omr_config_data['glorytun_udp_type'] == 'static':
            glorytun_udp_host_ip = '10.255.254.1'
            glorytun_udp_client_ip = '10.255.254.2'
        else:
            glorytun_udp_host_ip = 'dhcp'
            glorytun_udp_client_ip = 'dhcp'
    else:
        glorytun_udp_host_ip = '10.255.254.1'
        glorytun_udp_client_ip = '10.255.254.2'
    available_vpn = ["glorytun-tcp", "glorytun-udp"]
    if os.path.isfile('/etc/dsvpn/dsvpn.key'):
        dsvpn_key = open('/etc/dsvpn/dsvpn.key').readline().rstrip()
        available_vpn.append("dsvpn")
    else:
        dsvpn_key = ''
    dsvpn_port = '65011'
    dsvpn_host_ip = '10.255.251.1'
    dsvpn_client_ip = '10.255.251.2'

    if os.path.isfile('/etc/iperf3/public.pem'):
        with open('/etc/iperf3/public.pem',"rb") as iperfkey_file:
            iperf_keyb = base64.b64encode(iperfkey_file.read())
            iperf3_key = iperf_keyb.decode('utf-8')
    else:
        iperf3_key = ''

    if os.path.isfile('/etc/pihole/setupVars.conf'):
        pihole = True
    else:
        pihole = False

    #if os.path.isfile('/etc/openvpn/server/static.key'):
    #    with open('/etc/openvpn/server/static.key',"rb") as ovpnkey_file:
    #        openvpn_keyb = base64.b64encode(ovpnkey_file.read())
    #        openvpn_key = openvpn_keyb.decode('utf-8')
    #    available_vpn.append("openvpn")
    #else:
    #    openvpn_key = ''
    openvpn_key = ''
    if os.path.isfile('/etc/openvpn/client/client.key'):
        with open('/etc/openvpn/client/client.key',"rb") as ovpnkey_file:
            openvpn_keyb = base64.b64encode(ovpnkey_file.read())
            openvpn_client_key = openvpn_keyb.decode('utf-8')
    else:
        openvpn_client_key = ''
    if os.path.isfile('/etc/openvpn/client/client.crt'):
        with open('/etc/openvpn/client/client.crt',"rb") as ovpnkey_file:
            openvpn_keyb = base64.b64encode(ovpnkey_file.read())
            openvpn_client_crt = openvpn_keyb.decode('utf-8')
        available_vpn.append("openvpn")
    else:
        openvpn_client_crt = ''
    if os.path.isfile('/etc/openvpn/server/ca.crt'):
        with open('/etc/openvpn/server/ca.crt',"rb") as ovpnkey_file:
            openvpn_keyb = base64.b64encode(ovpnkey_file.read())
            openvpn_client_ca = openvpn_keyb.decode('utf-8')
    else:
        openvpn_client_ca = ''
    openvpn_port = '65301'
    if os.path.isfile('/etc/openvpn/openvpn-tun0.conf'):
        with open('/etc/openvpn/openvpn-tun0.conf',"r") as openvpn_file:
            for line in openvpn_file:
                if 'port ' in line:
                    openvpn_port = line.replace(line[:5], '').rstrip()
    openvpn_host_ip = '10.255.252.1'
    #openvpn_client_ip = '10.255.252.2'
    openvpn_client_ip = 'dhcp'

    if os.path.isfile('/etc/mlvpn/mlvpn0.conf'):
        mlvpn_config = configparser.ConfigParser()
        mlvpn_config.read_file(open(r'/etc/mlvpn/mlvpn0.conf'))
        mlvpn_key = mlvpn_config.get('general','password').strip('"')
        available_vpn.append("mlvpn")
    else:
        mlvpn_key = ''
    mlvpn_host_ip = '10.255.253.1'
    mlvpn_client_ip = '10.255.253.2'


    mptcp_enabled = os.popen('sysctl -n net.mptcp.mptcp_enabled').read().rstrip()
    mptcp_checksum = os.popen('sysctl -n net.mptcp.mptcp_checksum').read().rstrip()
    mptcp_path_manager = os.popen('sysctl -n  net.mptcp.mptcp_path_manager').read().rstrip()
    mptcp_scheduler = os.popen('sysctl -n net.mptcp.mptcp_scheduler').read().rstrip()
    mptcp_syn_retries = os.popen('sysctl -n net.mptcp.mptcp_syn_retries').read().rstrip()

    congestion_control = os.popen('sysctl -n net.ipv4.tcp_congestion_control').read().rstrip()

    ipv6_network = os.popen('ip -6 addr show ' + iface +' | grep -oP "(?<=inet6 ).*(?= scope global)"').read().rstrip()
    #ipv6_addr = os.popen('wget -6 -qO- -T 2 ipv6.openmptcprouter.com').read().rstrip()
    ipv6_addr = os.popen('ip -6 addr show ' + iface +' | grep -oP "(?<=inet6 ).*(?= scope global)" | cut -d/ -f1').read().rstrip()
    #ipv4_addr = os.popen('wget -4 -qO- -T 1 https://ip.openmptcprouter.com').read().rstrip()
    ipv4_addr = os.popen("dig -4 TXT +timeout=2 +tries=1 +short o-o.myaddr.l.google.com @ns1.google.com | awk -F'\"' '{ print $2}'").read().rstrip()
    if ipv4_addr == '':
        ipv4_addr = os.popen('wget -4 -qO- -T 1 http://ifconfig.co').read().rstrip()
    #ipv4_addr = ""

    test_aes = os.popen('cat /proc/cpuinfo | grep aes').read().rstrip()
    if test_aes == '':
        vps_aes = False
    else:
        vps_aes = True
    vps_kernel = os.popen('uname -r').read().rstrip()
    vps_machine = os.popen('uname -m').read().rstrip()
    vps_omr_version = os.popen("grep -s 'OpenMPTCProuter VPS' /etc/* | awk '{print $4}'").read().rstrip()
    vps_loadavg = os.popen("cat /proc/loadavg | awk '{print $1" "$2" "$3}'").read().rstrip()
    vps_uptime = os.popen("cat /proc/uptime | awk '{print $1}'").read().rstrip()
    vps_domain = os.popen('wget -4 -qO- -T 1 http://hostname.openmptcprouter.com').read().rstrip()
    #vps_domain = os.popen('dig -4 +short +times=3 +tries=1 -x ' + ipv4_addr + " | sed 's/\.$//'").read().rstrip()

    vpn = ''
    if os.path.isfile('/etc/openmptcprouter-vps-admin/current-vpn'):
        vpn = os.popen('cat /etc/openmptcprouter-vps-admin/current-vpn').read().rstrip()
    if vpn == '':
        vpn = 'glorytun-tcp'

    shorewall_redirect = "enable"
    with open('/etc/shorewall/rules','r') as f:
        for line in f:
            if '#DNAT		net		vpn:$OMR_ADDR	tcp	1-64999' in line:
                shorewall_redirect = "disable"

    return {'vps': {'kernel': vps_kernel,'machine': vps_machine,'omr_version': vps_omr_version,'loadavg': vps_loadavg,'uptime': vps_uptime,'aes': vps_aes},'shadowsocks': {'key': shadowsocks_key,'port': shadowsocks_port,'method': shadowsocks_method,'fast_open': shadowsocks_fast_open,'reuse_port': shadowsocks_reuse_port,'no_delay': shadowsocks_no_delay,'mptcp': shadowsocks_mptcp,'ebpf': shadowsocks_ebpf,'obfs': shadowsocks_obfs,'obfs_plugin': shadowsocks_obfs_plugin,'obfs_type': shadowsocks_obfs_type},'glorytun': {'key': glorytun_key,'udp': {'host_ip': glorytun_udp_host_ip,'client_ip': glorytun_udp_client_ip},'tcp': {'host_ip': glorytun_tcp_host_ip,'client_ip': glorytun_tcp_client_ip},'port': glorytun_port,'chacha': glorytun_chacha},'dsvpn': {'key': dsvpn_key, 'host_ip': dsvpn_host_ip, 'client_ip': dsvpn_client_ip, 'port': dsvpn_port},'openvpn': {'key': openvpn_key,'client_key': openvpn_client_key,'client_crt': openvpn_client_crt,'client_ca': openvpn_client_ca,'host_ip': openvpn_host_ip, 'client_ip': openvpn_client_ip, 'port': openvpn_port},'mlvpn': {'key': mlvpn_key, 'host_ip': mlvpn_host_ip, 'client_ip': mlvpn_client_ip},'shorewall': {'redirect_ports': shorewall_redirect},'mptcp': {'enabled': mptcp_enabled,'checksum': mptcp_checksum,'path_manager': mptcp_path_manager,'scheduler': mptcp_scheduler, 'syn_retries': mptcp_syn_retries},'network': {'congestion_control': congestion_control,'ipv6_network': ipv6_network,'ipv6': ipv6_addr,'ipv4': ipv4_addr,'domain': vps_domain},'vpn': {'available': available_vpn,'current': vpn},'iperf': {'user': 'openmptcprouter','password': 'openmptcprouter', 'key': iperf3_key},'pihole': {'state': pihole}}

# Set shadowsocks config
class ShadowsocksConfigparams(BaseModel):
    port: int
    method: str
    fast_open: bool
    reuse_port: bool
    no_delay: bool
    mptcp: bool
    obfs: bool
    obfs_plugin: str
    obfs_type: str
    key: str

#@app.post('/shadowsocks')
#def shadowsocks(*,params: ShadowsocksConfigparams,current_user: User = Depends(get_current_user)):
#    with open('/etc/shadowsocks-libev/config.json') as f:
#        content = f.read()
#    content = re.sub(",\s*}","}",content)
#    try:
#        data = json.loads(content)
#    except ValueError as e:
#        data = {'timeout': 600, 'verbose': 0, 'prefer_ipv6': False}
#    if 'timeout' in data:
#        timeout = data["timeout"]
#    if 'verbose' in data:
#        verbose = data["verbose"]
#    else:
#        verbose = 0
#    prefer_ipv6 = data["prefer_ipv6"]
#    port = params.port
#    method = params.method
#    fast_open = params.fast_open
#    reuse_port = params.reuse_port
#    no_delay = params.no_delay
#    mptcp = params.mptcp
#    obfs = params.obfs
#    obfs_plugin = params.obfs_plugin
#    obfs_type = params.obfs_type
#    ebpf = params.ebpf
#    key = params.key
#    if not key:
#        if 'key' in data:
#            key = data["key"]
#    vps_domain = os.popen('wget -4 -qO- -T 2 http://hostname.openmptcprouter.com').read().rstrip()
#
#    if port is None or method is None or fast_open is None or reuse_port is None or no_delay is None or key is None:
#        return {'result': 'error','reason': 'Invalid parameters','route': 'shadowsocks'}
#    if obfs:
#        if obfs_plugin == 'v2ray':
#            if obfs_type == 'tls':
#                if vps_domain == '':
#                    shadowsocks_config = {'server': '::0','server_port': port,'local_port': 1081,'mode': 'tcp_and_udp','key': key,'timeout': timeout,'method': method,'verbose': verbose,'ipv6_first': True, 'prefer_ipv6': prefer_ipv6,'fast_open': fast_open,'no_delay': no_delay,'reuse_port': reuse_port,'mptcp': mptcp,'ebpf': ebpf,'acl': '/etc/shadowsocks-libev/local.acl', 'plugin': '/usr/local/bin/v2ray-plugin','plugin_opts': 'server;tls'}
#                else:
#                    shadowsocks_config = {'server': '::0','server_port': port,'local_port': 1081,'mode': 'tcp_and_udp','key': key,'timeout': timeout,'method': method,'verbose': verbose,'ipv6_first': True, 'prefer_ipv6': prefer_ipv6,'fast_open': fast_open,'no_delay': no_delay,'reuse_port': reuse_port,'mptcp': mptcp,'ebpf': ebpf,'acl': '/etc/shadowsocks-libev/local.acl', 'plugin': '/usr/local/bin/v2ray-plugin','plugin_opts': 'server;tls;host=' + vps_domain}
#            else:
#                shadowsocks_config = {'server': '::0','server_port': port,'local_port': 1081,'mode': 'tcp_and_udp','key': key,'timeout': timeout,'method': method,'verbose': verbose,'ipv6_first': True, 'prefer_ipv6': prefer_ipv6,'fast_open': fast_open,'no_delay': no_delay,'reuse_port': reuse_port,'mptcp': mptcp,'ebpf': ebpf,'acl': '/etc/shadowsocks-libev/local.acl', 'plugin': '/usr/local/bin/v2ray-plugin','plugin_opts': 'server'}
#        else:
#            if obfs_type == 'tls':
#                if vps_domain == '':
#                    shadowsocks_config = {'server': ('[::0]', '0.0.0.0'),'server_port': port,'local_port': 1081,'mode': 'tcp_and_udp','key': key,'timeout': timeout,'method': method,'verbose': verbose,'ipv6_first': True, 'prefer_ipv6': prefer_ipv6,'fast_open': fast_open,'no_delay': no_delay,'reuse_port': reuse_port,'mptcp': mptcp,'ebpf': ebpf,'acl': '/etc/shadowsocks-libev/local.acl', 'plugin': '/usr/local/bin/obfs-server','plugin_opts': 'obfs=tls;mptcp;fast-open;t=400'}
#                else:
#                    shadowsocks_config = {'server': ('[::0]', '0.0.0.0'),'server_port': port,'local_port': 1081,'mode': 'tcp_and_udp','key': key,'timeout': timeout,'method': method,'verbose': verbose,'ipv6_first': True, 'prefer_ipv6': prefer_ipv6,'fast_open': fast_open,'no_delay': no_delay,'reuse_port': reuse_port,'mptcp': mptcp,'ebpf': ebpf,'acl': '/etc/shadowsocks-libev/local.acl', 'plugin': '/usr/local/bin/obfs-server','plugin_opts': 'obfs=tls;mptcp;fast-open;t=400;host=' + vps_domain}
#            else:
#                shadowsocks_config = {'server': ('[::0]', '0.0.0.0'),'server_port': port,'local_port': 1081,'mode': 'tcp_and_udp','key': key,'timeout': timeout,'method': method,'verbose': verbose,'ipv6_first': True, 'prefer_ipv6': prefer_ipv6,'fast_open': fast_open,'no_delay': no_delay,'reuse_port': reuse_port,'mptcp': mptcp,'ebpf': ebpf,'acl': '/etc/shadowsocks-libev/local.acl', 'plugin': '/usr/local/bin/obfs-server','plugin_opts': 'obfs=http;mptcp;fast-open;t=400'}
#    else:
#        shadowsocks_config = {'server': ('[::0]', '0.0.0.0'),'server_port': port,'local_port': 1081,'mode': 'tcp_and_udp','key': key,'timeout': timeout,'method': method,'verbose': verbose,'ipv6_first': True, 'prefer_ipv6': prefer_ipv6,'fast_open': fast_open,'no_delay': no_delay,'reuse_port': reuse_port,'mptcp': mptcp,'ebpf': ebpf,'acl': '/etc/shadowsocks-libev/local.acl'}
#
#    if ordered(data) != ordered(json.loads(json.dumps(shadowsocks_config))):
#        with open('/etc/shadowsocks-libev/config.json','w') as outfile:
#            json.dump(shadowsocks_config,outfile,indent=4)
#        os.system("systemctl restart shadowsocks-libev-server@config.service")
#        for x in range (1,os.cpu_count()):
#            os.system("systemctl restart shadowsocks-libev-server@config" + str(x) + ".service")
#        shorewall_add_port(str(port),'tcp','shadowsocks')
#        shorewall_add_port(str(port),'udp','shadowsocks')
#        set_lastchange()
#        return {'result': 'done','reason': 'changes applied','route': 'shadowsocks'}
#    else:
#        return {'result': 'done','reason': 'no changes','route': 'shadowsocks'}

@app.post('/shadowsocks')
def shadowsocks(*,params: ShadowsocksConfigparams,current_user: User = Depends(get_current_user)):
    ipv6_network = os.popen('ip -6 addr show ' + iface +' | grep -oP "(?<=inet6 ).*(?= scope global)"').read().rstrip()
    with open('/etc/shadowsocks-libev/manager.json') as f:
        content = f.read()
    content = re.sub(",\s*}","}",content)
    try:
        data = json.loads(content)
    except ValueError as e:
        data = {'timeout': 600, 'verbose': 0, 'prefer_ipv6': False}
    #key = data["key"]
    if 'timeout' in data:
        timeout = data["timeout"]
    if 'verbose' in data:
        verbose = data["verbose"]
    else:
        verbose = 0
    prefer_ipv6 = data["prefer_ipv6"]
    port = params.port
    method = params.method
    fast_open = params.fast_open
    reuse_port = params.reuse_port
    no_delay = params.no_delay
    mptcp = params.mptcp
    obfs = params.obfs
    obfs_plugin = params.obfs_plugin
    obfs_type = params.obfs_type
    ebpf = 0
    key = params.key
    portkey = data["port_key"]
    portkey[str(port)] = key
    #ipv4_addr = os.popen('wget -4 -qO- -T 2 http://ip.openmptcprouter.com').read().rstrip()
    vps_domain = os.popen('wget -4 -qO- -T 2 http://hostname.openmptcprouter.com').read().rstrip()

    if port is None or method is None or fast_open is None or reuse_port is None or no_delay is None or key is None:
        return {'result': 'error','reason': 'Invalid parameters','route': 'shadowsocks'}
    if ipv6_network == '':
        if obfs:
            if obfs_plugin == "v2ray":
                if obfs_type == "tls":
                    if vps_domain == '':
                        shadowsocks_config = {'server': '0.0.0.0','port_key': portkey,'local_port': 1081,'mode': 'tcp_and_udp','timeout': timeout,'method': method,'verbose': verbose,'ipv6_first': True, 'prefer_ipv6': prefer_ipv6,'fast_open': fast_open,'no_delay': no_delay,'reuse_port': reuse_port,'mptcp': mptcp,'ebpf': ebpf,'acl': '/etc/shadowsocks-libev/local.acl', 'plugin': '/usr/local/bin/v2ray-plugin','plugin_opts': 'server;tls'}
                    else:
                        shadowsocks_config = {'server': '0.0.0.0','port_key': portkey,'local_port': 1081,'mode': 'tcp_and_udp','timeout': timeout,'method': method,'verbose': verbose,'ipv6_first': True, 'prefer_ipv6': prefer_ipv6,'fast_open': fast_open,'no_delay': no_delay,'reuse_port': reuse_port,'mptcp': mptcp,'ebpf': ebpf,'acl': '/etc/shadowsocks-libev/local.acl', 'plugin': '/usr/local/bin/v2ray-plugin','plugin_opts': 'server;tls;host=' + vps_domain}
                else:
                    shadowsocks_config = {'server': '0.0.0.0','port_key': portkey,'local_port': 1081,'mode': 'tcp_and_udp','timeout': timeout,'method': method,'verbose': verbose,'ipv6_first': True, 'prefer_ipv6': prefer_ipv6,'fast_open': fast_open,'no_delay': no_delay,'reuse_port': reuse_port,'mptcp': mptcp,'ebpf': ebpf,'acl': '/etc/shadowsocks-libev/local.acl', 'plugin': '/usr/local/bin/v2ray-plugin','plugin_opts': 'server'}
            else:
                if obfs_type == 'tls':
                    if vps_domain == '':
                        shadowsocks_config = {'server': '0.0.0.0','port_key': portkey,'local_port': 1081,'mode': 'tcp_and_udp','timeout': timeout,'method': method,'verbose': verbose,'ipv6_first': True, 'prefer_ipv6': prefer_ipv6,'fast_open': fast_open,'no_delay': no_delay,'reuse_port': reuse_port,'mptcp': mptcp,'ebpf': ebpf,'acl': '/etc/shadowsocks-libev/local.acl', 'plugin': '/usr/local/bin/obfs-server','plugin_opts': 'obfs=tls;mptcp;fast-open;t=400'}
                    else:
                        shadowsocks_config = {'server': '0.0.0.0','port_key': portkey,'local_port': 1081,'mode': 'tcp_and_udp','timeout': timeout,'method': method,'verbose': verbose,'ipv6_first': True, 'prefer_ipv6': prefer_ipv6,'fast_open': fast_open,'no_delay': no_delay,'reuse_port': reuse_port,'mptcp': mptcp,'ebpf': ebpf,'acl': '/etc/shadowsocks-libev/local.acl', 'plugin': '/usr/local/bin/obfs-server','plugin_opts': 'obfs=tls;mptcp;fast-open;t=400;host=' + vps_domain}
                else:
                    shadowsocks_config = {'server': '0.0.0.0','port_key': portkey,'local_port': 1081,'mode': 'tcp_and_udp','timeout': timeout,'method': method,'verbose': verbose,'ipv6_first': True, 'prefer_ipv6': prefer_ipv6,'fast_open': fast_open,'no_delay': no_delay,'reuse_port': reuse_port,'mptcp': mptcp,'ebpf': ebpf,'acl': '/etc/shadowsocks-libev/local.acl', 'plugin': '/usr/local/bin/obfs-server','plugin_opts': 'obfs=http;mptcp;fast-open;t=400'}
        else:
            shadowsocks_config = {'server': '0.0.0.0','port_key': portkey,'local_port': 1081,'mode': 'tcp_and_udp','timeout': timeout,'method': method,'verbose': verbose,'ipv6_first': True, 'prefer_ipv6': prefer_ipv6,'fast_open': fast_open,'no_delay': no_delay,'reuse_port': reuse_port,'mptcp': mptcp,'ebpf': ebpf,'acl': '/etc/shadowsocks-libev/local.acl'}
    else:
        if obfs:
            if obfs_plugin == "v2ray":
                if obfs_type == "tls":
                    if vps_domain == '':
                        shadowsocks_config = {'server': '::0','port_key': portkey,'local_port': 1081,'mode': 'tcp_and_udp','timeout': timeout,'method': method,'verbose': verbose,'ipv6_first': True, 'prefer_ipv6': prefer_ipv6,'fast_open': fast_open,'no_delay': no_delay,'reuse_port': reuse_port,'mptcp': mptcp,'ebpf': ebpf,'acl': '/etc/shadowsocks-libev/local.acl', 'plugin': '/usr/local/bin/v2ray-plugin','plugin_opts': 'server;tls'}
                    else:
                        shadowsocks_config = {'server': '::0','port_key': portkey,'local_port': 1081,'mode': 'tcp_and_udp','timeout': timeout,'method': method,'verbose': verbose,'ipv6_first': True, 'prefer_ipv6': prefer_ipv6,'fast_open': fast_open,'no_delay': no_delay,'reuse_port': reuse_port,'mptcp': mptcp,'ebpf': ebpf,'acl': '/etc/shadowsocks-libev/local.acl', 'plugin': '/usr/local/bin/v2ray-plugin','plugin_opts': 'server;tls;host=' + vps_domain}
                else:
                    shadowsocks_config = {'server': '::0','port_key': portkey,'local_port': 1081,'mode': 'tcp_and_udp','timeout': timeout,'method': method,'verbose': verbose,'ipv6_first': True, 'prefer_ipv6': prefer_ipv6,'fast_open': fast_open,'no_delay': no_delay,'reuse_port': reuse_port,'mptcp': mptcp,'ebpf': ebpf,'acl': '/etc/shadowsocks-libev/local.acl', 'plugin': '/usr/local/bin/v2ray-plugin','plugin_opts': 'server'}
            else:
                if obfs_type == 'tls':
                    if vps_domain == '':
                        shadowsocks_config = {'server': ('[::0]', '0.0.0.0'),'port_key': portkey,'local_port': 1081,'mode': 'tcp_and_udp','timeout': timeout,'method': method,'verbose': verbose,'ipv6_first': True, 'prefer_ipv6': prefer_ipv6,'fast_open': fast_open,'no_delay': no_delay,'reuse_port': reuse_port,'mptcp': mptcp,'ebpf': ebpf,'acl': '/etc/shadowsocks-libev/local.acl', 'plugin': '/usr/local/bin/obfs-server','plugin_opts': 'obfs=tls;mptcp;fast-open;t=400'}
                    else:
                        shadowsocks_config = {'server': ('[::0]', '0.0.0.0'),'port_key': portkey,'local_port': 1081,'mode': 'tcp_and_udp','timeout': timeout,'method': method,'verbose': verbose,'ipv6_first': True, 'prefer_ipv6': prefer_ipv6,'fast_open': fast_open,'no_delay': no_delay,'reuse_port': reuse_port,'mptcp': mptcp,'ebpf': ebpf,'acl': '/etc/shadowsocks-libev/local.acl', 'plugin': '/usr/local/bin/obfs-server','plugin_opts': 'obfs=tls;mptcp;fast-open;t=400;host=' + vps_domain}
                else:
                    shadowsocks_config = {'server': ('[::0]', '0.0.0.0'),'port_key': portkey,'local_port': 1081,'mode': 'tcp_and_udp','timeout': timeout,'method': method,'verbose': verbose,'ipv6_first': True, 'prefer_ipv6': prefer_ipv6,'fast_open': fast_open,'no_delay': no_delay,'reuse_port': reuse_port,'mptcp': mptcp,'ebpf': ebpf,'acl': '/etc/shadowsocks-libev/local.acl', 'plugin': '/usr/local/bin/obfs-server','plugin_opts': 'obfs=http;mptcp;fast-open;t=400'}
        else:
            shadowsocks_config = {'server': ('[::0]', '0.0.0.0'),'port_key': portkey,'local_port': 1081,'mode': 'tcp_and_udp','timeout': timeout,'method': method,'verbose': verbose,'ipv6_first': True, 'prefer_ipv6': prefer_ipv6,'fast_open': fast_open,'no_delay': no_delay,'reuse_port': reuse_port,'mptcp': mptcp,'ebpf': ebpf,'acl': '/etc/shadowsocks-libev/local.acl'}

    if ordered(data) != ordered(json.loads(json.dumps(shadowsocks_config))):
        with open('/etc/shadowsocks-libev/manager.json','w') as outfile:
            json.dump(shadowsocks_config,outfile,indent=4)
        os.system("systemctl restart shadowsocks-libev-manager@manager.service")
        for x in range (1,os.cpu_count()):
            os.system("systemctl restart shadowsocks-libev-manager@manager" + str(x) + ".service")
        shorewall_add_port(str(port),'tcp','shadowsocks')
        shorewall_add_port(str(port),'udp','shadowsocks')
        set_lastchange()
        return {'result': 'done','reason': 'changes applied','route': 'shadowsocks'}
    else:
        return {'result': 'done','reason': 'no changes','route': 'shadowsocks'}

# Set shorewall config
class ShorewallAllparams(BaseModel):
    redirect_ports: str

@app.post('/shorewall')
def shorewall(*, params: ShorewallAllparams,current_user: User = Depends(get_current_user)):
    state = params.redirect_ports
    if state is None:
        return {'result': 'error','reason': 'Invalid parameters','route': 'shorewall'}
    initial_md5 = hashlib.md5(file_as_bytes(open('/etc/shorewall/rules', 'rb'))).hexdigest()
    fd, tmpfile = mkstemp()
    with open('/etc/shorewall/rules','r') as f, open(tmpfile,'a+') as n:
        for line in f:
            if state == 'enable' and line == '#DNAT		net		vpn:$OMR_ADDR	tcp	1-64999\n':
                n.write(line.replace(line[:1], ''))
            elif state == 'enable' and line == '#DNAT		net		vpn:$OMR_ADDR	udp	1-64999\n':
                n.write(line.replace(line[:1], ''))
            elif state == 'disable' and line == 'DNAT		net		vpn:$OMR_ADDR	tcp	1-64999\n':
                n.write('#' + line)
            elif state == 'disable' and line == 'DNAT		net		vpn:$OMR_ADDR	udp	1-64999\n':
                n.write('#' + line)
            else:
                n.write(line)
    os.close(fd)
    move(tmpfile,'/etc/shorewall/rules')
    final_md5 = hashlib.md5(file_as_bytes(open('/etc/shorewall/rules', 'rb'))).hexdigest()
    if not initial_md5 == final_md5:
        os.system("systemctl -q reload shorewall")
    # Need to do the same for IPv6...
    return {'result': 'done','reason': 'changes applied'}

class ShorewallListparams(BaseModel):
    name: str

@app.post('/shorewalllist')
def shorewall_list(*,params: ShorewallListparams, current_user: User = Depends(get_current_user)):
    name = params.name
    if name is None:
        return {'result': 'error','reason': 'Invalid parameters','route': 'shorewalllist'}
    fwlist = []
    with open('/etc/shorewall/rules','r') as f:
        for line in f:
            if '# OMR ' + name in line:
                fwlist.append(line)
    return {'list': fwlist}

class Shorewallparams(BaseModel):
    name: str
    port: str
    proto: str
    fwtype: str

@app.post('/shorewallopen')
def shorewall_open(*,params: Shorewallparams, current_user: User = Depends(get_current_user)):
    name = params.name
    port = params.port
    proto = params.proto
    fwtype = params.fwtype
    if name is None:
        return {'result': 'error','reason': 'Invalid parameters','route': 'shorewalllist'}
    shorewall_add_port(str(port),proto,name,fwtype)
    return {'result': 'done','reason': 'changes applied'}

@app.post('/shorewallclose')
def shorewall_close(*,params: Shorewallparams,current_user: User = Depends(get_current_user)):
    name = params.name
    port = params.port
    proto = params.proto
    fwtype = params.fwtype
    if name is None:
        return {'result': 'error','reason': 'Invalid parameters','route': 'shorewalllist'}
    shorewall_del_port(str(port),proto,name,fwtype)
    return {'result': 'done','reason': 'changes applied'}

# Set MPTCP config
class MPTCPparams(BaseModel):
    checksum: str
    path_manager: str
    scheduler: str
    syn_retries: int
    congestion_control: str

@app.post('/mptcp')
def mptcp(*, params: MPTCPparams,current_user: User = Depends(get_current_user)):
    checksum = params.checksum
    path_manager = params.path_manager
    scheduler = params.scheduler
    syn_retries = params.syn_retries
    congestion_control = params.congestion_control
    if not checksum or not path_manager or not scheduler or not syn_retries or not congestion_control:
        return {'result': 'error','reason': 'Invalid parameters','route': 'mptcp'}
    os.system('sysctl -qw net.mptcp.mptcp_checksum=' + checksum)
    os.system('sysctl -qw net.mptcp.mptcp_path_manager=' + path_manager)
    os.system('sysctl -qw net.mptcp.mptcp_scheduler=' + scheduler)
    os.system('sysctl -qw net.mptcp.mptcp_syn_retries=' + syn_retries)
    os.system('sysctl -qw net.ipv4.tcp_congestion_control=' + congestion_control)
    set_lastchange()
    return {'result': 'done','reason': 'changes applied'}

class Vpn(BaseModel):
    vpn: str

# Set global VPN config
@app.post('/vpn')
def vpn(*,vpnconfig: Vpn,current_user: User = Depends(get_current_user)):
    vpn = vpnconfig.vpn
    if not vpn:
        return {'result': 'error','reason': 'Invalid parameters','route': 'vpn'}
    os.system('echo ' + vpn + ' > /etc/openmptcprouter-vps-admin/current-vpn')
    set_lastchange()
    return {'result': 'done','reason': 'changes applied'}


class GlorytunConfig(BaseModel):
    key: str
    port: int
    chacha: bool

# Set Glorytun config
@app.post('/glorytun')
def glorytun(*, glorytunconfig: GlorytunConfig,current_user: User = Depends(get_current_user)):
    key = glorytunconfig.key
    port = glorytunconfig.port
    chacha = glorytunconfig.chacha
    initial_md5 = hashlib.md5(file_as_bytes(open('/etc/glorytun-tcp/tun0', 'rb'))).hexdigest()
    with open('/etc/glorytun-tcp/tun0.key','w') as outfile:
        outfile.write(key)
    with open('/etc/glorytun-udp/tun0.key','w') as outfile:
        outfile.write(key)
    fd, tmpfile = mkstemp()
    with open('/etc/glorytun-tcp/tun0','r') as f, open(tmpfile,'a+') as n:
        for line in f:
            if 'PORT=' in line:
                n.write('PORT=' + str(port) + '\n')
            elif 'OPTIONS=' in line:
                if chacha:
                    n.write('OPTIONS="chacha20 retry count -1 const 5000000 timeout 90000 keepalive count 5 idle 10 interval 2 buffer-size 65536 multiqueue"\n')
                else:
                    n.write('OPTIONS="retry count -1 const 5000000 timeout 90000 keepalive count 5 idle 10 interval 2 buffer-size 65536 multiqueue"\n')
            else:
                n.write(line)
    os.close(fd)
    move(tmpfile,'/etc/glorytun-tcp/tun0')
    final_md5 = hashlib.md5(file_as_bytes(open('/etc/glorytun-tcp/tun0', 'rb'))).hexdigest()
    if not initial_md5 == final_md5:
        os.system("systemctl -q restart glorytun-tcp@tun0")
    initial_md5 = hashlib.md5(file_as_bytes(open('/etc/glorytun-udp/tun0', 'rb'))).hexdigest()
    fd, tmpfile = mkstemp()
    with open('/etc/glorytun-udp/tun0','r') as f, open(tmpfile,'a+') as n:
        for line in f:
            if 'BIND_PORT=' in line:
                n.write('BIND_PORT=' + str(port) + '\n')
            elif 'OPTIONS=' in line:
                if chacha:
                    n.write('OPTIONS="chacha persist"\n')
                else:
                    n.write('OPTIONS="persist"\n')
            else:
                n.write(line)
    os.close(fd)
    move(tmpfile,'/etc/glorytun-udp/tun0')
    final_md5 = hashlib.md5(file_as_bytes(open('/etc/glorytun-udp/tun0', 'rb'))).hexdigest()
    if not initial_md5 == final_md5:
        os.system("systemctl -q restart glorytun-udp@tun0")
    shorewall_add_port(str(port),'tcp','glorytun')
    set_lastchange()
    return {'result': 'done'}

# Set A Dead Simple VPN config
class DSVPN(BaseModel):
    key: str
    port: int

@app.post('/dsvpn')
def dsvpn(*,params: DSVPN,current_user: User = Depends(get_current_user)):
    key = params.key
    port = params.port
    if not key or port is None:
        return {'result': 'error','reason': 'Invalid parameters','route': 'dsvpn'}
    initial_md5 = hashlib.md5(file_as_bytes(open('/etc/dsvpn/dsvpn.key', 'rb'))).hexdigest()
    with open('/etc/dsvpn/dsvpn.key','w') as outfile:
        outfile.write(key)
    final_md5 = hashlib.md5(file_as_bytes(open('/etc/dsvpn/dsvpn.key', 'rb'))).hexdigest()
    if not initial_md5 == final_md5:
        os.system("systemctl -q restart dsvpn-server")
    shorewall_add_port(str(port),'tcp','dsvpn')
    set_lastchange()
    return {'result': 'done'}

# Set OpenVPN config
class OpenVPN(BaseModel):
    key: str

@app.post('/openvpn')
def openvpn(*,ovpn: OpenVPN,current_user: User = Depends(get_current_user)):
    key = ovpn.key
    if not key:
        return {'result': 'error','reason': 'Invalid parameters','route': 'openvpn'}
    initial_md5 = hashlib.md5(file_as_bytes(open('/etc/openvpn/server/static.key', 'rb'))).hexdigest()
    with open('/etc/openvpn/server/static.key','w') as outfile:
        outfile.write(base64.b64decode(key))
    final_md5 = hashlib.md5(file_as_bytes(open('/etc/openvpn/server/static.key', 'rb'))).hexdigest()
    if not initial_md5 == final_md5:
        os.system("systemctl -q restart openvpn@tun0")
    set_lastchange()
    return {'result': 'done'}

class Wanips(BaseModel):
    ips: str

# Set WANIP
@app.post('/wan')
def wan(*, wanips: Wanips,current_user: User = Depends(get_current_user)):
    ips = wanips.ips
    if not ips:
        return {'result': 'error','reason': 'Invalid parameters','route': 'wan'}
    initial_md5 = hashlib.md5(file_as_bytes(open('/etc/shadowsocks-libev/local.acl', 'rb'))).hexdigest()
    with open('/etc/shadowsocks-libev/local.acl','w') as outfile:
        outfile.write('[white_list]\n')
        outfile.write(ips)
    final_md5 = hashlib.md5(file_as_bytes(open('/etc/shadowsocks-libev/local.acl', 'rb'))).hexdigest()
    #if not initial_md5 == final_md5:
        #os.system("systemctl restart shadowsocks-libev-server@config.service")
        #for x in range (1,os.cpu_count()):
            #os.system("systemctl restart shadowsocks-libev-server@config" + str(x) + ".service")

    return {'result': 'done'}

# Update VPS
@app.get('/update')
def update(current_user: User = Depends(get_current_user)):
    os.system("wget -O - http://www.openmptcprouter.com/server/debian9-x86_64.sh | sh")
    # Need to reboot if kernel change
    return {'result': 'done'}

# Backup
class Backupfile(BaseModel):
    data: str

@app.post('/backuppost')
def backuppost(*,backupfile: Backupfile ,current_user: User = Depends(get_current_user)):
    backup_file = backupfile.data
    if not backup_file:
        return {'result': 'error','reason': 'Invalid parameters','route': 'backuppost'}
    with open('/var/opt/openmptcprouter/backup.tar.gz','wb') as f:
        f.write(base64.b64decode(backup_file))
    return {'result': 'done'}

@app.get('/backupget')
def send_backup(current_user: User = Depends(get_current_user)):
    with open('/var/opt/openmptcprouter/backup.tar.gz',"rb") as backup_file:
        file_base64 = base64.b64encode(backup_file.read())
        file_base64utf = file_base64.decode('utf-8')
    return {'data': file_base64utf}

@app.get('/backuplist')
def list_backup(current_user: User = Depends(get_current_user)):
    if os.path.isfile('/var/opt/openmptcprouter/backup.tar.gz'):
        modiftime = os.path.getmtime('/var/opt/openmptcprouter/backup.tar.gz')
        return {'backup': True, 'modif': modiftime}
    else:
        return {'backup': False}

@app.get('/backupshow')
def show_backup(current_user: User = Depends(get_current_user)):
    if os.path.isfile('/var/opt/openmptcprouter/backup.tar.gz'):
        router = OpenWrt(native=open('/var/opt/openmptcprouter/backup.tar.gz'))
        return {'backup': True,'data': router}
    else:
        return {'backup': False}

@app.post('/backupedit')
def edit_backup(params,current_user: User = Depends(get_current_user)):
    o = OpenWrt(params)
    o.write('backup',path='/var/opt/openmptcprouter/')
    return {'result': 'done'}


if __name__ == '__main__':
    with open('/etc/openmptcprouter-vps-admin/omr-admin-config.json') as f:
        omr_config_data = json.load(f)
    omrport=65500
    if 'port' in omr_config_data:
        omrport = omr_config_data["port"]
    uvicorn.run(app,host='0.0.0.0',port=omrport,log_level='debug',ssl_certfile='/etc/openmptcprouter-vps-admin/cert.pem',ssl_keyfile='/etc/openmptcprouter-vps-admin/key.pem')
#    uvicorn.run(app,host='0.0.0.0',port=omrport,ssl_context=('/etc/openmptcprouter-vps-admin/cert.pem','/etc/openmptcprouter-vps-admin/key.pem'),threaded=True)
