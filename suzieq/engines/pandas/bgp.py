from suzieq.sqobjects.address import AddressObj

import pandas as pd
import numpy as np

from .engineobj import SqPandasEngine
from suzieq.utils import build_query_str, SchemaForTable, humanize_timestamp


class BgpObj(SqPandasEngine):

    @staticmethod
    def table_name():
        return 'bgp'

    def get(self, **kwargs):
        """Replacing the original interface name in returned result"""

        addnl_fields = kwargs.pop('addnl_fields', [])
        columns = kwargs.get('columns', ['default'])
        vrf = kwargs.pop('vrf', None)
        peer = kwargs.pop('peer', None)
        hostname = kwargs.pop('hostname', None)
        user_query = kwargs.pop('query_str', None)

        drop_cols = ['origPeer', 'peerHost']
        addnl_fields.extend(['origPeer'])
        sch = SchemaForTable(self.iobj.table, self.schemas)
        fields = sch.get_display_fields(columns)

        for col in ['peerIP', 'updateSource', 'state', 'namespace', 'vrf',
                    'peer', 'hostname']:
            if col not in fields:
                addnl_fields.append(col)
                drop_cols.append(col)

        try:
            df = super().get(addnl_fields=addnl_fields, **kwargs)
        except KeyError as ex:
            if ('afi' in str(ex)) or ('safi' in str(ex)):
                df = pd.DataFrame(
                    {'error': [f'ERROR: Migrate BGP data first using sq-coalescer']})
                return df

        if df.empty:
            return df

        if 'afiSafi' in columns or (columns == ['*']):
            df['afiSafi'] = df['afi'] + ' ' + df['safi']
        query_str = build_query_str([], sch, vrf=vrf, peer=peer,
                                    hostname=hostname)
        if 'peer' in df.columns:
            df['peer'] = np.where(df['origPeer'] != "",
                                  df['origPeer'], df['peer'])

        # Convert old data into new 2.0 data format
        if 'peerHostname' in df.columns:
            mdf = self._get_peer_matched_df(df)
            drop_cols = [x for x in drop_cols if x in mdf.columns]
            drop_cols.extend(list(mdf.filter(regex='_y')))
        else:
            mdf = df

        mdf = self._handle_user_query_str(mdf, user_query)

        if query_str:
            return mdf.query(query_str).drop(columns=drop_cols,
                                             errors='ignore')
        else:
            return mdf.drop(columns=drop_cols, errors='ignore')

    def summarize(self, **kwargs) -> pd.DataFrame:
        """Summarize key information about BGP"""

        self._init_summarize(self.iobj._table, **kwargs)
        if self.summary_df.empty or ('error' in self.summary_df.columns):
            return self.summary_df

        self.summary_df['afiSafi'] = (
            self.summary_df['afi'] + ' ' + self.summary_df['safi'])

        afi_safi_count = self.summary_df.groupby(by=['namespace'])['afiSafi'] \
                                        .nunique()

        self.summary_df = self.summary_df \
                              .set_index(['namespace', 'hostname', 'vrf',
                                          'peer']) \
                              .query('~index.duplicated(keep="last")') \
                              .reset_index()
        self.ns = {i: {} for i in self.summary_df['namespace'].unique()}
        self.nsgrp = self.summary_df.groupby(by=["namespace"],
                                             observed=True)

        self._summarize_on_add_field = [
            ('deviceCnt', 'hostname', 'nunique'),
            ('totalPeerCnt', 'peer', 'count'),
            ('uniqueAsnCnt', 'asn', 'nunique'),
            ('uniqueVrfsCnt', 'vrf', 'nunique')
        ]

        self._summarize_on_add_with_query = [
            ('failedPeerCnt', 'state == "NotEstd"', 'peer'),
            ('iBGPPeerCnt', 'asn == peerAsn', 'peer'),
            ('eBGPPeerCnt', 'asn != peerAsn', 'peer'),
            ('rrClientPeerCnt', 'rrclient.str.lower() == "true"', 'peer',
             'count'),
        ]

        self._gen_summarize_data()

        {self.ns[i].update({'activeAfiSafiCnt': afi_safi_count[i]})
         for i in self.ns.keys()}
        self.summary_row_order.append('activeAfiSafiCnt')

        self.summary_df['estdTime'] = humanize_timestamp(
            self.summary_df.estdTime,
            self.cfg.get('analyzer', {}).get('timezone', None))

        self.summary_df['estdTime'] = (
            self.summary_df['timestamp'] - self.summary_df['estdTime'])
        self.summary_df['estdTime'] = self.summary_df['estdTime'] \
                                          .apply(lambda x: x.round('s'))
        # Now come the BGP specific ones
        established = self.summary_df.query("state == 'Established'") \
            .groupby(by=['namespace'])

        uptime = established["estdTime"]
        rx_updates = established["updatesRx"]
        tx_updates = established["updatesTx"]
        self._add_stats_to_summary(uptime, 'upTimeStat')
        self._add_stats_to_summary(rx_updates, 'updatesRxStat')
        self._add_stats_to_summary(tx_updates, 'updatesTxStat')

        self.summary_row_order.extend(['upTimeStat', 'updatesRxStat',
                                       'updatesTxStat'])

        self._post_summarize()
        return self.ns_df.convert_dtypes()

    def _get_peer_matched_df(self, df) -> pd.DataFrame:
        """Get a BGP dataframe that also contains a session's matching peer"""

        if 'peerHostname' not in df.columns:
            return df

        # We have to separate out the Established and non Established entries
        # for the merge. Otherwise we end up with a mess
        df_1 = df[['namespace', 'hostname', 'vrf', 'peer', 'peerIP',
                   'updateSource']] \
            .drop_duplicates() \
            .reset_index(drop=True)
        df_2 = df[['namespace', 'hostname', 'vrf', 'updateSource']] \
            .drop_duplicates() \
            .reset_index(drop=True)

        mdf = df_1.merge(df_2,
                         left_on=['namespace', 'peerIP'],
                         right_on=['namespace', 'updateSource'],
                         suffixes=('', '_y')) \
            .drop_duplicates(subset=['namespace', 'hostname', 'vrf',
                                     'peerIP']) \
            .rename(columns={'hostname_y': 'peerHost'}) \
            .fillna(value={'peerHostname': '', 'peerHost': ''}) \
            .reset_index(drop=True)

        df = df.merge(mdf[['namespace', 'hostname', 'vrf', 'peer', 'peerHost']],
                      on=['namespace', 'hostname', 'vrf', 'peer'], how='left')

        df['peerHostname'] = np.where((df['peerHostname'] == '') &
                                      (df['state'] == "Established"),
                                      df['peerHost'],
                                      df['peerHostname'])
        df = df.drop(columns=['peerHost'])

        for i in df.select_dtypes(include='category'):
            df[i].cat.add_categories('', inplace=True)

        return df

    def aver(self, **kwargs) -> pd.DataFrame:
        """BGP Assert"""

        assert_cols = ["namespace", "hostname", "vrf", "peer", "peerHostname",
                       "afi", "safi", "asn", "state", "peerAsn", "bfdStatus",
                       "reason", "notificnReason", "afisAdvOnly",
                       "afisRcvOnly", "peerIP", "updateSource"]

        kwargs.pop("columns", None)  # Loose whatever's passed
        status = kwargs.pop("status", 'all')

        df = self.get(columns=assert_cols, state='!dynamic', **kwargs)
        if 'error' in df:
            return df

        if df.empty:
            if status != "pass":
                df['assert'] = 'fail'
                df['assertReason'] = 'No data'
            return df

        df = self._get_peer_matched_df(df)
        if df.empty:
            if status != "pass":
                df['assert'] = 'fail'
                df['assertReason'] = 'No data'
            return df

        # We can get rid of sessions with duplicate peer info since we're
        # interested only in session info here
        df = df.drop_duplicates(
            subset=['namespace', 'hostname', 'vrf', 'peer'])
        df['assertReason'] = [[] for _ in range(len(df))]

        df['assertReason'] += df.apply(lambda x: ["asn mismatch"]
                                       if x['state'] != "Established" and
                                       (x['peerHostname'] and
                                        ((x["asn"] != x["peerAsn_y"]) or
                                         (x['asn_y'] != x['peerAsn'])))
                                       else [], axis=1)

        # Get list of peer IP addresses for peer not in Established state
        # Returning to performing checks even if we didn't get LLDP/Intf info
        df['assertReason'] += df.apply(
            lambda x: [f"{x['reason']}:{x['notificnReason']}"]
            if ((x['state'] != 'Established') and
                (x['reason'] and x['reason'] != 'None' and
                 x['reason'] != "No error"))
            else [],
            axis=1)

        df['assertReason'] += df.apply(
            lambda x: ['Matching BGP Peer not found']
            if x['state'] == 'NotEstd' and not x['assertReason']
            else [],  axis=1)

        df['assertReason'] += df.apply(
            lambda x: ['Not all Afi/Safis enabled']
            if x['afisAdvOnly'].any() or x['afisRcvOnly'].any() else [],
            axis=1)

        df['assert'] = df.apply(lambda x: 'pass'
                                if not len(x.assertReason) else 'fail',
                                axis=1)

        result = df[['namespace', 'hostname', 'vrf', 'peer', 'asn',
                     'peerAsn', 'state', 'peerHostname', 'assert',
                     'assertReason', 'timestamp']] \
            .explode(column="assertReason") \
            .fillna({'assertReason': '-'})

        if status == "fail":
            return result.query('assertReason != "-"')
        elif status == "pass":
            return result.query('assertReason == "-"')

        return result
