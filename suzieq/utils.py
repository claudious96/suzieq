import os
import re
import sys
from typing import Tuple
from pathlib import Path
import logging
from logging.handlers import RotatingFileHandler
import json
import yaml
from dateutil.relativedelta import relativedelta
from datetime import datetime
from tzlocal import get_localzone
from pytz import all_timezones
import fcntl
from importlib.util import find_spec
import errno
from dateparser import parse

import pandas as pd
import pyarrow as pa


logger = logging.getLogger(__name__)


MAX_MTU = 9216


def validate_sq_config(cfg):
    """Validate Suzieq config file

    Parameters:
    -----------
    cfg: yaml object, YAML encoding of the config file

    Returns:
    --------
    status: None if all is good or error string
    """

    ddir = cfg.get("data-directory", None)
    if not ddir:
        return "FATAL: No data directory for output files specified"

    if not os.path.isdir(ddir):
        if not ddir.startswith('s3'):
            os.makedirs(ddir, exist_ok=True)
        else:
            return f'FATAL: Data directory {ddir} is not a directory'

    if not (os.access(ddir, os.R_OK | os.W_OK | os.EX_OK)):
        return f'FATAL: Data directory {ddir} has wrong permissions'

    # Locate the service and schema directories
    svcdir = cfg.get('service-directory', None)
    if (not (svcdir and os.path.isdir(ddir) and
             os.access(svcdir, os.R_OK | os.W_OK | os.EX_OK))):
        sqdir = get_sq_install_dir()
        svcdir = f'{sqdir}/config'
        if os.access(svcdir, os.R_OK | os.EX_OK):
            cfg['service-directory'] = svcdir
        else:
            svcdir = None

    if not svcdir:
        return 'FATAL: No service directory found'

    schemadir = cfg.get('schema-directory', None)
    if not (schemadir and os.access(schemadir, os.R_OK | os.EX_OK)):
        schemadir = f'{svcdir}/schema'
        if os.access(schemadir, os.R_OK | os.EX_OK):
            cfg['schema-directory'] = schemadir
        else:
            schemadir = None

    if not schemadir:
        return 'FATAL: No schema directory found'

    # Move older format logging level and period to appropriate new location
    if 'poller' not in cfg:
        cfg['poller'] = {}

    for knob in ['logging-level', 'period']:
        if knob in cfg:
            cfg['poller'][knob] = cfg[knob]

    if 'rest' not in cfg:
        cfg['rest'] = {}

    for knob in ['API_KEY', 'rest_certfile', 'rest_keyfile']:
        if knob in cfg:
            cfg['rest'][knob] = cfg[knob]

    # Verify timezone if present is valid
    def_tz = get_localzone().zone
    reader = cfg.get('analyzer', {})
    if reader and isinstance(reader, dict):
        usertz = reader.get('timezone', '')
        if usertz and usertz not in all_timezones:
            return f'Invalid timezone: {usertz}'
        elif not usertz:
            reader['timezone'] = def_tz
    else:
        cfg['analyzer'] = {'timezone': def_tz}

    return None


def load_sq_config(validate=True, config_file=None):
    """Load (and validate) basic suzieq config"""

    # Order of looking up suzieq config:
    #   Current directory
    #   ${HOME}/.suzieq/

    cfgfile = None
    cfg = None

    cfgfile = sq_get_config_file(config_file)

    if cfgfile:
        try:
            with open(cfgfile, "r") as f:
                cfg = yaml.safe_load(f.read())
        except Exception as e:
            print(f'ERROR: Unable to open config file {cfgfile}: {e.args[1]}')
            sys.exit(1)

        if not cfg:
            print(f'ERROR: Empty config file {cfgfile}')
            sys.exit(1)

        if validate:
            error_str = validate_sq_config(cfg)
            if error_str:
                print(f'ERROR: Invalid config file: {config_file}')
                print(error_str)
                sys.exit(1)

    if not cfg:
        print(f"suzieq requires a configuration file either in ./suzieq-cfg.yml "
              "or ~/suzieq/suzieq-cfg.yml")
        sys.exit(1)

    return cfg


def sq_get_config_file(config_file):
    """Get the path to the suzieq config file"""

    if config_file:
        cfgfile = config_file
    elif os.path.exists("./suzieq-cfg.yml"):
        cfgfile = "./suzieq-cfg.yml"
    elif os.path.exists(os.getenv("HOME") + "/.suzieq/suzieq-cfg.yml"):
        cfgfile = os.getenv("HOME") + "/.suzieq/suzieq-cfg.yml"
    return cfgfile


class Schema(object):
    def __init__(self, schema_dir):
        self._init_schemas(schema_dir)
        if not self._schema:
            raise ValueError("Unable to get schemas")

    def _init_schemas(self, schema_dir: str):
        """Returns the schema definition which is the fields of a table"""

        schemas = {}
        phy_tables = {}
        types = {}

        if not (schema_dir and os.path.exists(schema_dir)):
            logger.error(
                "Schema directory {} does not exist".format(schema_dir))
            raise Exception(f"Schema directory {schema_dir} does not exist")

        for root, _, files in os.walk(schema_dir):
            for topic in files:
                if not topic.endswith(".avsc"):
                    continue
                with open(root + "/" + topic, "r") as f:
                    data = json.loads(f.read())
                    table = data["name"]
                    schemas[table] = data["fields"]
                    types[table] = data.get('recordType', 'record')
                    phy_tables[data["name"]] = data.get("physicalTable", table)
            break

        self._schema = schemas
        self._phy_tables = phy_tables
        self._types = types

    def tables(self):
        return self._schema.keys()

    def fields_for_table(self, table):
        return [f['name'] for f in self._schema[table]]

    def get_raw_schema(self, table):
        return self._schema[table]

    def field_for_table(self, table, field):
        for f in self._schema[table]:
            if f['name'] == field:
                return f

    def type_for_table(self, table):
        return self._types[table]

    def key_fields_for_table(self, table):
        # return [f['name'] for f in self._schema[table] if f.get('key', None) is not None]
        return self._sort_fields_for_table(table, 'key')

    def augmented_fields_for_table(self, table):
        return [x['name'] for x in self._schema.get(table, [])
                if 'depends' in x]

    def sorted_display_fields_for_table(self, table, getall=False):
        return self._sort_fields_for_table(table, 'display', getall)

    def _sort_fields_for_table(self, table, tag, getall=False):
        fields = self.fields_for_table(table)
        field_weights = {}
        for f_name in fields:
            field = self.field_for_table(table, f_name)
            if field.get(tag, None) is not None:
                field_weights[f_name] = field.get(tag, 1000)
            elif getall:
                field_weights[f_name] = 1000
        return [k for k in sorted(field_weights.keys(),
                                  key=lambda x: field_weights[x])]

    def array_fields_for_table(self, table):
        fields = self.fields_for_table(table)
        arrays = []
        for f_name in fields:
            field = self.field_for_table(table, f_name)
            if isinstance(field['type'], dict) and field['type'].get('type', None) == 'array':
                arrays.append(f_name)
        return arrays

    def get_phy_table_for_table(self, table):
        """Return the name of the underlying physical table"""
        if self._phy_tables:
            return self._phy_tables.get(table, table)

    def get_partition_columns_for_table(self, table):
        """Return the list of partition columns for table"""
        if self._phy_tables:
            return self._sort_fields_for_table(table, 'partition')

    def get_arrow_schema(self, table):
        """Convert the internal AVRO schema into the equivalent PyArrow schema"""

        avro_sch = self._schema.get(table, None)
        if not avro_sch:
            raise AttributeError(f"No schema found for {table}")

        arsc_fields = []

        map_type = {
            "string": pa.string(),
            "long": pa.int64(),
            "int": pa.int32(),
            "double": pa.float64(),
            "float": pa.float32(),
            "timestamp": pa.int64(),
            "timedelta64[s]": pa.float64(),
            "boolean": pa.bool_(),
            "array.string": pa.list_(pa.string()),
            "array.nexthopList": pa.list_(pa.struct([('nexthop', pa.string()),
                                                     ('oif', pa.string()),
                                                     ('weight', pa.int32())])),
            "array.long": pa.list_(pa.int64()),
            "array.float": pa.list_(pa.float32()),
        }

        for fld in avro_sch:
            if "depends" in fld:
                # These are augmented fields, not in arrow"
                continue
            if isinstance(fld["type"], dict):
                if fld["type"]["type"] == "array":
                    if fld["type"]["items"]["type"] == "record":
                        avtype: str = "array.{}".format(fld["name"])
                    else:
                        avtype: str = "array.{}".format(
                            fld["type"]["items"]["type"])
                else:
                    # We don't support map yet
                    raise AttributeError
            else:
                avtype: str = fld["type"]

            arsc_fields.append(pa.field(fld["name"], map_type[avtype]))

        return pa.schema(arsc_fields)

    def get_parent_fields(self, table, field):
        avro_sch = self._schema.get(table, None)
        if not avro_sch:
            raise AttributeError(f"No schema found for {table}")

        for fld in avro_sch:
            if fld['name'] == field:
                if "depends" in fld:
                    return fld['depends'].split()
                else:
                    return []
        return []


class SchemaForTable(object):
    def __init__(self, table, schema: Schema = None, schema_dir=None):
        if schema:
            if isinstance(schema, Schema):
                self._all_schemas = schema
            else:
                raise ValueError("Passing non-Schema type for schema")
        else:
            self._all_schemas = Schema(schema_dir=schema_dir)
        self._table = table
        if table not in self._all_schemas.tables():
            raise ValueError(f"Unknown table {table}, no schema found for it")

    @property
    def type(self):
        return self._all_schemas.type_for_table(self._table)

    @property
    def version(self):
        return self._all_schemas.field_for_table(self._table,
                                                 'sqvers')['default']

    @property
    def fields(self):
        return self._all_schemas.fields_for_table(self._table)

    def get_phy_table(self):
        return self._all_schemas.get_phy_table_for_table(self._table)

    def get_partition_columns(self):
        return self._all_schemas.get_partition_columns_for_table(self._table)

    def key_fields(self):
        return self._all_schemas.key_fields_for_table(self._table)

    def get_augmented_fields(self):
        return self._all_schemas.augmented_fields_for_table(self._table)

    def sorted_display_fields(self, getall=False):
        return self._all_schemas.sorted_display_fields_for_table(self._table,
                                                                 getall)

    @property
    def array_fields(self):
        return self._all_schemas.array_fields_for_table(self._table)

    def field(self, field):
        return self._all_schemas.field_for_table(self._table, field)

    def get_display_fields(self, columns: list) -> list:
        """Return the list of display fields for the given table"""
        if columns == ["default"]:
            fields = self.sorted_display_fields()

            if "namespace" not in fields:
                fields.insert(0, "namespace")
        elif columns == ["*"]:
            fields = self.sorted_display_fields(getall=True)
        else:
            fields = [f for f in columns if f in self.fields]

        return fields

    def get_phy_table_for_table(self) -> str:
        """Return the underlying physical table for this logical table"""
        return self._all_schemas.get_phy_table_for_table(self._table)

    def get_raw_schema(self):
        return self._all_schemas.get_raw_schema(self._table)

    def get_arrow_schema(self):
        return self._all_schemas.get_arrow_schema(self._table)

    def get_parent_fields(self, field):
        return self._all_schemas.get_parent_fields(self._table, field)


def get_latest_files(folder, start="", end="", view="latest") -> list:
    lsd = []

    if start:
        ssecs = pd.to_datetime(
            start, infer_datetime_format=True).timestamp() * 1000
    else:
        ssecs = 0

    if end:
        esecs = pd.to_datetime(
            end, infer_datetime_format=True).timestamp() * 1000
    else:
        esecs = 0

    ts_dirs = False
    pq_files = False

    for root, dirs, files in os.walk(folder):
        flst = None
        if dirs and dirs[0].startswith("timestamp") and not pq_files:
            flst = get_latest_ts_dirs(dirs, ssecs, esecs, view)
            ts_dirs = True
        elif files and not ts_dirs:
            flst = get_latest_pq_files(files, root, ssecs, esecs, view)
            pq_files = True

        if flst:
            lsd.append(os.path.join(root, flst[-1]))

    return lsd


def get_latest_ts_dirs(dirs, ssecs, esecs, view):
    newdirs = None

    if not ssecs and not esecs:
        dirs.sort(key=lambda x: int(x.split("=")[1]))
        newdirs = dirs
    elif ssecs and not esecs:
        newdirs = list(filter(lambda x: int(x.split("=")[1]) > ssecs, dirs))
        if not newdirs and view != "changes":
            # FInd the entry most adjacent to this one
            newdirs = list(filter(lambda x: int(
                x.split("=")[1]) < ssecs, dirs))
    elif esecs and not ssecs:
        newdirs = list(filter(lambda x: int(x.split("=")[1]) < esecs, dirs))
    else:
        newdirs = list(
            filter(
                lambda x: int(x.split("=")[1]) < esecs and int(
                    x.split("=")[1]) > ssecs,
                dirs,
            )
        )
        if not newdirs and view != "changes":
            # FInd the entry most adjacent to this one
            newdirs = list(filter(lambda x: int(
                x.split("=")[1]) < ssecs, dirs))

    return newdirs


def get_latest_pq_files(files, root, ssecs, esecs, view):

    newfiles = None

    if not ssecs and not esecs:
        files.sort(key=lambda x: os.path.getctime("%s/%s" % (root, x)))
        newfiles = files
    elif ssecs and not esecs:
        newfiles = list(
            filter(lambda x: os.path.getctime(
                "%s/%s" % (root, x)) > ssecs, files)
        )
        if not newfiles and view != "changes":
            # FInd the entry most adjacent to this one
            newfiles = list(
                filter(
                    lambda x: os.path.getctime(
                        "{}/{}".format(root, x)) < ssecs, files
                )
            )
    elif esecs and not ssecs:
        newfiles = list(
            filter(lambda x: os.path.getctime(
                "%s/%s" % (root, x)) < esecs, files)
        )
    else:
        newfiles = list(
            filter(
                lambda x: os.path.getctime("%s/%s" % (root, x)) < esecs
                and os.path.getctime("%s/%s" % (root, x)) > ssecs,
                files,
            )
        )
        if not newfiles and view != "changes":
            # Find the entry most adjacent to this one
            newfiles = list(
                filter(lambda x: os.path.getctime(
                    "%s/%s" % (root, x)) < ssecs, files)
            )
    return newfiles


def calc_avg(oldval, newval):
    if not oldval:
        return newval

    return float((oldval+newval)/2)


def get_timestamp_from_cisco_time(input, timestamp):
    """Get timestamp in ms from the Cisco-specific timestamp string
    Examples of Cisco timestamp str are P2DT14H45M16S, P1M17DT4H49M50S etc.
    """
    if not input.startswith('P'):
        return 0
    months = days = hours = mins = secs = 0

    if 'T' in input:
        day, timestr = input[1:].split('T')
    else:
        day = input[1:]
        timestr = ''

    if 'Y' in day:
        years, day = day.split('Y')
        months = int(years)*12

    if 'M' in day:
        mnt, day = day.split('M')
        months = months + int(mnt)
    if 'D' in day:
        days = int(day.split('D')[0])

    if 'H' in timestr:
        hours, timestr = timestr.split('H')
        hours = int(hours)
    if 'M' in timestr:
        mins, timestr = timestr.split('M')
        mins = int(mins)
    if 'S' in timestr:
        secs = timestr.split('S')[0]
        secs = int(secs)

    delta = relativedelta(months=months, days=days,
                          hours=hours, minutes=mins, seconds=secs)
    return int((datetime.fromtimestamp(timestamp)-delta).timestamp()*1000)


def get_timestamp_from_junos_time(input, timestamp: int):
    """Get timestamp in ms from the Junos-specific timestamp string
    The expected input looks like: "attributes" : {"junos:seconds" : "0"}.
    We don't check for format because we're assuming the input would be blank
    if it wasn't the right format. The input can either be a dictionary or a
    JSON string.
    """

    if not input:
        # Happens for logical interfaces such as gr-0/0/0
        secs = 0
    else:
        try:
            if isinstance(input, str):
                data = json.loads(input)
            else:
                data = input
            secs = int(data.get('junos:seconds', 0))
        except Exception:
            logger.warning(f'Unable to convert junos secs from {input}')
            secs = 0

    delta = relativedelta(seconds=int(secs))
    return int((datetime.fromtimestamp(timestamp)-delta).timestamp()*1000)


def convert_macaddr_format_to_colon(macaddr: str) -> str:
    """Convert NXOS/EOS . macaddr form to standard : format, lowecase

    :param macaddr: str, the macaddr string to convert
    :returns: the converted macaddr string or all 0s string if arg not str
    :rtype: str

    """
    if isinstance(macaddr, str):
        if re.match(r'[0-9a-zA-Z]{4}.[0-9a-zA-Z]{4}.[0-9a-zA-Z]{4}', macaddr):
            return (':'.join([f'{x[:2]}:{x[2:]}'
                              for x in macaddr.split('.')])).lower()
        else:
            return macaddr.lower()

    return('00:00:00:00:00:00')


def convert_rangestring_to_list(rangestr: str) -> list:
    """Convert a range list such as '1, 2-5, 10, 12-20' to list
    """

    tmplst = []
    if not isinstance(rangestr, str):
        return tmplst

    try:
        for x in rangestr.split(','):
            x = x.strip().split('-')
            if x[0]:
                if len(x) == 2:
                    intrange = list(range(int(x[0]), int(x[1])+1))
                    tmplst.extend(intrange)
                else:
                    tmplst.append(int(x[0]))
    except Exception:
        logger.error(f"Range string parsing failed for {rangestr}")
        return []
    return tmplst


def build_query_str(skip_fields: list, schema, **kwargs) -> str:
    """Build a pandas query string given key/val pairs
    """
    query_str = ''
    prefix = ''

    def build_query_str(fld, fldtype) -> str:
        """Builds the string from the provided user input"""

        if ((fldtype == "long" or fldtype == "float") and not
                isinstance(fld, str)):
            result = f'=={fld}'

        elif fld.startswith('!'):
            fld = fld[1:]
            if fldtype == "long" or fldtype == "float":
                result = f'!= {fld}'
            else:
                result = f'!= "{fld}"'
        elif fld.startswith('<') or fld.startswith('>'):
            result = fld
        else:
            result = f'=="{fld}"'

        return result

    for f, v in kwargs.items():
        if not v or f in skip_fields or f in ["groupby"]:
            continue
        type = schema.field(f).get('type', 'string')
        if isinstance(v, list) and len(v):
            subq = ''
            subcond = ''
            for elem in v:
                subq += f'{subcond} {f}{build_query_str(elem, type)} '
                subcond = 'or'
            query_str += '{} ({})'.format(prefix, subq)
            prefix = "and"
        else:
            query_str += f'{prefix} {f}{build_query_str(v, type)} '
            prefix = "and"

    return query_str


def get_log_params(prog: str, cfg: dict, def_logfile: str) -> tuple:
    """Get the log file, level and size for the given program from config

    The logfile is supposed to be defined by a variable called logfile
    within the hierarchy of the config dictionary. Thus, the poller log file
    will be {'poller': {'logfile': '/tmp/sq-poller.log'}}, for example.

    :param prog: str, The name of the program. Valid values are poller, 
                      coaelscer, and rest.
    :param cfg: dict, The config dictionary
    :param def_logfile: str, The default log file to return
    :returns: log file name, log level, log size
    :rtype: str, str and int

    """
    if cfg:
        logfile = cfg.get(prog, {}).get('logfile', def_logfile)
        loglevel = cfg.get(prog, {}).get('logging-level', 'WARNING')
        logsize = cfg.get(prog, {}).get('logsize', 10000000)
    else:
        logfile = def_logfile
        loglevel = 'WARNING'
        logsize = 10000000

    return logfile, loglevel, logsize


def init_logger(logname: str,
                logfile: str,
                loglevel: str = 'WARNING',
                logsize: int = 10000000,
                use_stdout: bool = False) -> logging.Logger:
    """Initialize the logger

    :param logname: str, the name of the app that's logging
    :param logfile: str, the log file to use
    :param loglevel: str, the default log level to set the logger to
    :param use_stdout: str, log to stdout instead of or in addition to file

    """

    # this needs to be suzieq.poller, so that it is the root of all the other pollers
    logger = logging.getLogger(logname)
    logger.setLevel(loglevel.upper())
    fh = RotatingFileHandler(logfile, maxBytes=logsize, backupCount=2)
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s " "- %(message)s"
    )
    fh.setFormatter(formatter)

    # set root logger level, so that we set asyncssh log level
    #  asynchssh sets it's level to the root level
    root = logging.getLogger()
    root.setLevel(loglevel.upper())
    root.addHandler(fh)

    logger.warning(f"log level {logging.getLevelName(logger.level)}")

    return logger


def known_devtypes() -> list:
    """Returns the list of known dev types"""
    return(['cumulus', 'eos', 'iosxe', 'iosxr', 'ios', 'junos-mx', 'junos-qfx',
            'junos-ex', 'junos-es', 'linux', 'nxos', 'sonic'])


def humanize_timestamp(field: pd.Series, tz=None) -> pd.Series:
    '''Convert the UTC timestamp in Dataframe to local time.
    Use of pd.to_datetime will not work as it converts the timestamp
    to UTC. If the timestamp is already in UTC format, we get busted time.
    '''
    tz = tz or get_localzone().zone
    return field.apply(lambda x: datetime.utcfromtimestamp((int(x)/1000))) \
                .dt.tz_localize('UTC').dt.tz_convert(tz)


def expand_nxos_ifname(ifname: str) -> str:
    '''Expand shortned ifnames in NXOS to their full values, if required'''
    if not ifname:
        return ''
    if ifname.startswith('Eth') and 'Ether' not in ifname:
        return ifname.replace('Eth', 'Ethernet')
    elif ifname.startswith('Po') and 'port' not in ifname:
        return ifname.replace('Po', 'port-channel')
    return ifname


def expand_eos_ifname(ifname: str) -> str:
    '''Expand shortned ifnames in EOS to their full values, if required'''
    if not ifname:
        return ''
    if ifname.startswith('Eth') and 'Ether' not in ifname:
        return ifname.replace('Eth', 'Ethernet')
    elif ifname.startswith('Po') and 'Port' not in ifname:
        return ifname.replace('Po', 'Port-Channel')
    elif ifname.startswith('Vx') and 'Vxlan' not in ifname:
        return ifname.replace('Vx', 'Vxlan')
    return ifname


def ensure_single_instance(filename: str, block: bool = False) -> int:
    """Check there's only a single active instance of a process using lockfile

    It optionally can block waiting for the resource the become available.

    Use a pid file with advisory file locking to assure this.

    :returns: fd if lock was successful or 0
    :rtype: int

    """
    basedir = os.path.dirname(filename)
    if not os.path.exists(basedir):
        # Permission error or any other error will abort
        os.makedirs(basedir, exist_ok=True)

    fd = os.open(filename, os.O_RDWR | os.O_CREAT, 0o600)
    if fd:
        try:
            if block:
                fcntl.flock(fd, fcntl.LOCK_EX)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.truncate(fd, 0)
            os.write(fd, bytes(str(os.getpid()), 'utf-8'))
        except OSError:
            if OSError.errno == errno.EBUSY:
                # Return the PID of the process thats locked the file
                bpid = os.read(fd, 10)
                os.close(fd)
                try:
                    fd = -int(bpid)
                except ValueError:
                    fd = 0
            else:
                os.close(fd)
                fd = 0

    return fd


def expand_ios_ifname(ifname: str) -> str:
    """Get expanded interface name for IOSXR/XE given its short form

    :param ifname: str, short form of IOSXR interface name
    :returns: Expanded version of short form interface name
    :rtype: str
    """

    ifmap = {'BE': 'Bundle-Ether',
             'BV': 'BVI',
             'Fi': 'FiftyGigE',
             'Fo': 'FortyGigE',
             'FH': 'FourHundredGigE',
             'Gi': 'GigabitEthernet',
             'Gig': 'GigabitEthernet',
             'Hu': 'HundredGigE',
             'Lo': 'Loopback',
             'Mg': 'MgmtEth',
             'Nu': 'Null',
             'TE': 'TenGigE',
             'TF': 'TwentyFiveGigE',
             'TH': 'TwoHundredGigE',
             'tsec': 'tunnel-ipsec',
             'tmte': 'tunnel-mte',
             'tt': 'tunnel-te',
             'tp': 'tunnel-tp',
             'Vl': 'Vlan',
             'CPU': 'cpu',
             }
    pfx = re.match(r'[a-zA-Z]+', ifname)
    if pfx:
        pfxstr = pfx.group(0)
        if pfxstr in ifmap:
            return ifname.replace(pfxstr, ifmap[pfxstr])

    return ifname


def get_sq_install_dir() -> str:
    '''Return the absolute path of the suzieq installation dir'''
    spec = find_spec('suzieq')
    if spec:
        return(os.path.dirname(spec.loader.path))
    else:
        return(os.path.abspath('./'))


def get_sleep_time(period: str) -> int:
    """Returns the duration in seconds to sleep given a period

    Checking if the period format matches a specified format MUST be
    done by the caller.

    :param period: str, the period of form <value><unit>, '15m', '1h' etc
    :returns: duration to sleep in seconds
    :rtype: int
    """
    tm, unit, _ = re.split(r'(\D)', period)
    now = datetime.now()
    nextrun = parse(period, settings={'PREFER_DATES_FROM': 'future'})
    if unit == 'm':
        nextrun = nextrun.replace(second=0)
    elif unit == 'h':
        nextrun = nextrun.replace(minute=0, second=0)
    else:
        nextrun = nextrun.replace(hour=0, minute=0, second=0)

    return (nextrun-now).seconds
