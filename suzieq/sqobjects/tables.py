#!/usr/bin/env python3

# Copyright (c) Dinesh G Dutt
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.
#

import sys
import os
from pathlib import Path
import pandas as pd
from importlib import import_module

from suzieq.sqobjects import basicobj


class TablesObj(basicobj.SqObject):

    def get(self, **kwargs):
        '''Show the tables for which we have information'''
        tables = self.engine.get_tables(self.ctxt.cfg, **kwargs)
        df = pd.DataFrame()
        if tables:
            for i, table in enumerate(tables):
                module = import_module("suzieq.engines.pandas." + table)
                eobj = getattr(module, "{}Obj".format(table.title()))
                table_obj = eobj(self)
                info = {'table': table}
                info.update(table_obj.get_table_info(table))
                tables[i] = info

            df = pd.DataFrame.from_dict(tables)
            df = df.sort_values(by=['table']).reset_index(drop=True)
            cols = df.columns
            total = pd.DataFrame([['TOTAL', None, None, df['latest rows'].sum(), df['all rows'].sum(), df['intervals'].max(),
                                   df['datacenters'].max(), df['devices'].max()]], columns=cols)
            df = df.append(total, ignore_index=True)
        return df

    def summarize(self, **kwargs):
        "Describes the fields for a given table"

        df = None
        table = kwargs.get('table', '')
        if table not in self.schemas:
            raise LookupError('ERROR: Unknown table {}'.format(table))
        entries = [{'name': x['name'], 'type': x['type'], 'key': x.get('key', ''), 'display': x.get('display', '')}
                   for x in self.schemas[table]]
        df = pd.DataFrame.from_dict(entries)

        return(df)


if __name__ == '__main__':
    pass

