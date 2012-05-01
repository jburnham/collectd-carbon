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
import cPickle
import errno
import socket
from string import maketrans
import struct
from time import time
from traceback import format_exc

data = {}
host = None
port = None
differentiate_values = False
differentiate_values_over_time = False
lowercase_metric_names = False
prefix = None
types = {}
postfix = None
host_separator = "_"
metric_separator = "."
use_pickle_receiver = False
mangle_plugin = False

def carbon_parse_types_file(path):
    global types

    f = open(path, 'r')

    for line in f:
        fields = line.split()
        if len(fields) < 2:
            continue

        type_name = fields[0]

        if type_name[0] == '#':
            continue

        v = []
        for ds in fields[1:]:
            ds = ds.rstrip(',')
            ds_fields = ds.split(':')

            if len(ds_fields) != 4:
                collectd.warning('carbon_writer: cannot parse data source %s on type %s' % ( ds, type_name ))
                continue

            v.append(ds_fields)

        types[type_name] = v

    f.close()

def str_to_num(s):
    """
    Convert type limits from strings to floats for arithmetic.
    Will force U[nlimited] values to be 0.
    """

    try:
        n = float(s)
    except ValueError:
        n = 0

    return n

def sanitize_field(field):
    """
    Santize Metric Fields: replace dot and space with metric_separator. Delete
    parentheses. Convert to lower case if configured to do so.
    """
    field = field.strip()
    trans = maketrans(' .', metric_separator * 2)
    field = field.translate(trans, '()')
    if lowercase_metric_names:
        field = field.lower()
    return field

def carbon_config(c):
    global host, port, differentiate_values, differentiate_values_over_time, \
            prefix, postfix, host_separator, metric_separator, \
            lowercase_metric_names, use_pickle_receiver, mangle_plugin

    line_receiver_host_selected = False
    line_receiver_port_selected = False
    pickle_receiver_host_selected = False
    pickle_receiver_port_selected = False
    for child in c.children:
        if child.key == 'LineReceiverHost':
            host = child.values[0]
            line_receiver_host_selected = True
        elif child.key == 'LineReceiverPort':
            port = int(child.values[0])
            line_receiver_port_selected = True
        elif child.key == 'PickleReceiverHost':
            use_pickle_receiver = True
            host = child.values[0]
            pickle_receiver_host_selected = True
        elif child.key == 'PickleReceiverPort':
            port = child.values[0]
            pickle_receiver_port_selected = True
        elif child.key == 'TypesDB':
            for v in child.values:
                carbon_parse_types_file(v)
        # DeriveCounters maintained for backwards compatibility
        elif child.key == 'DeriveCounters':
            differentiate_values = True
        elif child.key == 'DifferentiateCounters':
            differentiate_values = True
        elif child.key == 'DifferentiateCountersOverTime':
            differentiate_values = True
            differentiate_values_over_time = True
        elif child.key == 'LowercaseMetricNames':
            lowercase_metric_names = True
        elif child.key == 'ManglePlugin':
            mangle_plugin = child.values[0]
        elif child.key == 'MetricPrefix':
            prefix = child.values[0]
        elif child.key == 'HostPostfix':
            postfix = child.values[0]
        elif child.key == 'HostSeparator':
            host_separator = child.values[0]
        elif child.key == 'MetricSeparator':
            metric_separator = child.values[0]

    if line_receiver_host_selected and pickle_receiver_host_selected:
        raise Exception('You cannot define both a Line and Pickle receiver host')

    if line_receiver_port_selected and pickle_receiver_port_selected:
        raise Exception('You cannot define both a Line and Pickle receiver port')

    if line_receiver_host_selected and not line_receiver_port_selected:
        raise Exception('You must define both a LineReceiverHost and LineReceiverPort')

    if pickle_receiver_host_selected and not pickle_receiver_port_selected:
        raise Exception('You must define both a PickleReceiverHost and PickleReceiverPort')

    if not host:
        raise Exception('You must define a Receiver Host')

    if not port:
        raise Exception('You must define a Receiver Port')

def carbon_init():
    import threading
    global data

    data = {
        'host': host,
        'port': port,
        'differentiate_values': differentiate_values,
        'differentiate_values_over_time': differentiate_values_over_time,
        'lowercase_metric_names': lowercase_metric_names,
        'buffer': [],
        'buffer_max_lines': 5000,
        'buffer_flush_interval': 60,
        'sock': None,
        'lock': threading.Lock(),
        'values': { },
        'last_connect_time': 0,
        'last_flush_time': 0,
        'mangle_plugin': __import__(mangle_plugin)
    }

    carbon_connect()

    collectd.register_write(carbon_write)

def carbon_connect():
    result = False
    global data

    if not data['sock']:
        # only attempt reconnect every 10 seconds
        now = time()
        if now - data['last_connect_time'] < 10:
            return False

        data['last_connect_time'] = now
        collectd.info('connecting to %s:%s' % ( data['host'], data['port'] ) )
        try:
            data['sock'] = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            data['sock'].connect((host, port))
            result = True
        except:
            result = False
            collectd.warning('error connecting socket: %s' % format_exc())
    else:
        result = True

    return result

def carbon_write_data():
    result = False
    global data
    data['lock'].acquire()
    now = time()
    if len(data['buffer']) < data['buffer_max_lines']:
        # don't flush the buffer if < buffer_flush_interval old
        if now - data['last_flush_time'] < data['buffer_flush_interval']:
            data['lock'].release()
            return result
    try:
        collectd.info("Buffer: %s" % len(data['buffer']))
        if use_pickle_receiver:
            serialized_data = cPickle.dumps(data['buffer'], protocol=-1)
            length_prefix = struct.pack("!L", len(serialized_data))
            s = length_prefix + serialized_data
        else:
            lines = []
            for metric in data['buffer']:
                lines.append('%s %f %d' % (metric[0], metric[1][1], metric[1][0]))
            s = '\n'.join(lines)
        data['buffer'] = []
        data['sock'].sendall(s)
        data['last_flush_time'] = now
        result = True
    except socket.error, e:
        data['sock'] = None
        if isinstance(e.args, tuple):
            collectd.warning('carbon_writer: socket error %d' % e[0])
        else:
            collectd.warning('carbon_writer: socket error')
    except:
        collectd.warning('carbon_writer: error sending data: %s' % format_exc())

    data['lock'].release()
    return result

def carbon_write(v):
    data['lock'].acquire()
    if not carbon_connect():
        data['lock'].release()
        collectd.warning('carbon_writer: no connection to carbon server')
        return

    data['lock'].release()

    if v.type not in types:
        collectd.warning('carbon_writer: do not know how to handle type %s. do you have all your types.db files configured?' % v.type)
        return

    v_type = types[v.type]

    if len(v_type) != len(v.values):
        collectd.warning('carbon_writer: differing number of values for type %s' % v.type)
        return

    if not mangle_plugin:
        metric_fields = []
        if prefix:
            metric_fields.append(prefix)

        metric_fields.append(v.host.replace('.', host_separator))

        if postfix is not None:
            metric_fields.append(postfix)

        metric_fields.append(v.plugin)
        if v.plugin_instance:
            metric_fields.append(sanitize_field(v.plugin_instance))

        metric_fields.append(v.type)
        if v.type_instance:
            metric_fields.append(sanitize_field(v.type_instance))
    else:
        metric_fields = data['mangle_plugin'].mangle_path(prefix, postfix, v)
        if metric_fields is None:
            return

    time = v.time

    # we update shared recorded values, so lock to prevent race conditions
    data['lock'].acquire()

    lines = []
    i = 0
    for value in v.values:
        ds_name = v_type[i][0]
        ds_type = v_type[i][1]
        path_fields = metric_fields[:]
        path_fields.append(ds_name)

        metric = '.'.join(path_fields)

        new_value = None

        # perform data normalization for COUNTER and DERIVE points
        if (isinstance(value, (float, int)) and
                data['differentiate_values'] and
                (ds_type == 'COUNTER' or ds_type == 'DERIVE')):
            # we have an old value
            if metric in data['values']:
                old_time, old_value = data['values'][metric]

                # overflow
                if value < old_value:
                    v_type_max = v_type[i][3]

                    if v_type_max == 'U':
                        # this is funky. pretend as if this is the first data point
                        new_value = None
                    else:
                        v_type_min = str_to_num(v_type[i][2])
                        v_type_max = str_to_num(v_type[i][3])
                        new_value = v_type_max - old_value + value - v_type_min
                else:
                    new_value = value - old_value

                if (isinstance(new_value, (float, int)) and
                        data['differentiate_values_over_time']):
                    interval = time - old_time
                    if interval < 1:
                        interval = 1
                    new_value = new_value / interval

            # update previous value
            data['values'][metric] = ( time, value )

        else:
            new_value = value

        if new_value is not None:
            data['buffer'].append([metric, [time, new_value]])

        i += 1

    data['lock'].release()

    carbon_write_data()

collectd.register_config(carbon_config)
collectd.register_init(carbon_init)
