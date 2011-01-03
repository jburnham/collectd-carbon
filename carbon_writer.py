#  Copyright 2010 Gregory Szorc
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
import collectd
import socket
from time import time

host = None
port = None
sock = None
types = {}
last_connect_time = 0

def carbon_parse_types_file(path):
    global types

    f = open(path, 'r')

    for line in f:
        fields = line.split()
        if len(fields) < 2:
            continue

        type_name = fields[0]
        v = []
        for ds in fields[1:]:
            ds = ds.rstrip(',')
            ds_fields = ds.split(':')

            if len(ds_fields) != 4:
                collectd.warning('invalid types.db data source type: %s' % ds)
                continue

            v.append(ds_fields)

        types[type_name] = v

    f.close()

def carbon_config(c):
    global host, port

    for child in c.children:
        if child.key == 'LineReceiverHost':
            host = child.values[0]
        elif child.key == 'LineReceiverPort':
            port = int(child.values[0])
        elif child.key == 'TypesFile':
            carbon_parse_types_file(child.values[0])

    if not host:
        raise Exception('LineReceiverHost not defined')

    if not port:
        raise Exception('LineReceiverPort not defined')

def carbon_connect():
    global host, port, sock, last_connect_time

    if not sock:
        if time() - last_connect_time > 10:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((host, port))
        else:
            collectd.info('waiting for timeout to attempt connection again')

    return sock != None

def carbon_write(v):
    # TODO try to reconnect gracefully
    if not carbon_connect():
        collectd.warning('no connection to carbon server')
        return

    if v.type not in types:
        collectd.warning('carbon module does not know how to handle type %s. do you have all your types.db files configured?' % v.type)
        return

    type = types[v.type]

    if len(type) != len(v.values):
        collectd.warning('differing number of values for type %s' % v.type)
        return

    metric_fields = [ v.host.replace('.', '_') ]

    s = v.plugin
    if v.plugin_instance:
        s = '-'.join([s, v.plugin_instance])
    metric_fields.append(s)

    s = v.type
    if v.type_instance:
        s = '-'.join([s, v.type_instance])
    metric_fields.append(s)

    time = v.time

    lines = []
    i = 0
    for value in v.values:
        ds_name = type[i][0]
        ds_type = type[i][1]
        i += 1

        path_fields = metric_fields[:]
        path_fields.append(ds_name)

        # TODO handle different data types here
        line = '%s %f %d' % ( '.'.join(path_fields), value, time )
        lines.append(line)

    lines.append('')
    sock.send('\n'.join(lines))

collectd.register_config(carbon_config)
collectd.register_init(carbon_init)
collectd.register_write(carbon_write)

