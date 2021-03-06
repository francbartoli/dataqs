#!/usr/bin/env python
# -*- coding: utf-8 -*-

###############################################################################
#  Copyright Kitware Inc. and Epidemico Inc.
#
#  Licensed under the Apache License, Version 2.0 ( the "License" );
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
###############################################################################

from __future__ import absolute_import
import json
import os
import datetime
import logging
from django.db import connections
from dataqs.processor_base import GeoDataProcessor, DEFAULT_WORKSPACE
from dataqs.helpers import postgres_query, ogr2ogr_exec, layer_exists, \
    style_exists
from geonode.geoserver.helpers import ogc_server_settings

logger = logging.getLogger("dataqs.processors")
script_dir = os.path.dirname(os.path.realpath(__file__))


class USGSQuakeProcessor(GeoDataProcessor):
    """
    Class for retrieving and processing the latest earthquake data from USGS.
    4 layers are created/updated with the same data (last 7 days by default),
    then any old data beyond the layer's time window (7 days, 30 days, etc)
    are removed.
    """
    prefix = 'usgs_quakes'
    tables = ("quakes_weekly", "quakes_monthly",
              "quakes_yearly", "quakes_archive")
    titles = ("Last 7 Days", "Last 30 Days", "Last 365 Days", "Archive")
    base_url = "http://earthquake.usgs.gov/fdsnws/event/1/query?" \
               "format=geojson&starttime={}&endtime={}"
    params = {}
    description = """Earthquake data from the US Geological Survey.
\n\nSource: http://earthquake.usgs.gov/fdsnws/event/1/
"""

    def __init__(self, *args, **kwargs):
        for key in kwargs.keys():
            self.params[key] = kwargs.get(key)

        if 'sdate' not in self.params:
            today = datetime.date.today()
            self.params['sdate'] = (
                today - datetime.timedelta(days=7)).strftime("%Y-%m-%d")

        if 'edate' not in self.params:
            today = datetime.date.today()
            self.params['edate'] = today.strftime("%Y-%m-%d")

        super(USGSQuakeProcessor, self).__init__(*args)

    def purge_old_data(self):
        """
        Remove old data from weekly, monthly, and yearly PostGIS tables
        """
        today = datetime.date.today()
        last_week = (today - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
        last_month = (today - datetime.timedelta(days=30)).strftime("%Y-%m-%d")
        last_year = (today - datetime.timedelta(days=365)).strftime("%Y-%m-%d")

        for interval, table in zip([last_week, last_month, last_year],
                                   self.tables):
            postgres_query(
                "DELETE FROM {} where CAST(time as timestamp) < '{}';".format(
                    table, interval), commit=True)

    def run(self, rss_file=None):
        """
        Retrieve the latest USGS earthquake data and append to all PostGIS
        earthquake tables, then remove old data
        :return:
        """
        if not rss_file:
            rss = self.download(self.base_url.format(self.params['sdate'],
                                                     self.params['edate']),
                                filename=self.prefix + '.rss')
            rss_file = os.path.join(self.tmp_dir, rss)

        json_data = None
        with open(rss_file) as json_file:
            json_data = json.load(json_file)
            for feature in json_data['features']:
                time_original = datetime.datetime.utcfromtimestamp(
                    feature['properties']['time']/1000)
                updated_original = datetime.datetime.utcfromtimestamp(
                    feature['properties']['updated']/1000)
                feature['properties']['time'] = time_original.strftime(
                    "%Y-%m-%d %H:%M:%S")
                feature['properties']['updated'] = updated_original.strftime(
                    "%Y-%m-%d %H:%M:%S")
        with open(rss_file, 'w') as modified_file:
            json.dump(json_data, modified_file)

        db = ogc_server_settings.datastore_db
        for table, title in zip(self.tables, self.titles):
            ogr2ogr_exec("-append -skipfailures -f PostgreSQL \
                \"PG:host={db_host} user={db_user} password={db_pass} \
                dbname={db_name}\" {rss} -nln {table}".format(
                db_host=db["HOST"], db_user=db["USER"], db_pass=db["PASSWORD"],
                db_name=db["NAME"], rss="{}".format(rss_file), table=table))
            datastore = ogc_server_settings.server.get('DATASTORE')
            if not layer_exists(table, datastore, DEFAULT_WORKSPACE):
                c = connections[datastore].cursor()
                q = 'ALTER TABLE {tb} ADD CONSTRAINT {tb}_ids UNIQUE (ids);'
                try:
                    c.execute(q.format(tb=table))
                except Exception:
                    c.close()
                self.post_geoserver_vector(table)
            if not style_exists(table):
                with open(os.path.join(
                        script_dir, 'resources/usgs.sld')) as sld:
                    self.set_default_style(table, table, sld.read())
            self.update_geonode(table,
                                title="Earthquakes - {}".format(title),
                                description=self.description,
                                store=datastore)
            self.truncate_gs_cache(table)
        self.purge_old_data()
        self.cleanup()


if __name__ == '__main__':
    processor = USGSQuakeProcessor()
    processor.run()
