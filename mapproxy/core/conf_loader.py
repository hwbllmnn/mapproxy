# This file is part of the MapProxy project.
# Copyright (C) 2010 Omniscale <http://omniscale.de>
# 
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
# 
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""
Configuration loading and system initializing.
"""
from __future__ import with_statement

import os
import yaml #pylint: disable-msg=F0401
import pkg_resources

import logging
log = logging.getLogger(__name__)

from mapproxy.core.srs import SRS
from mapproxy.core.odict import odict
from mapproxy.core.cache import FileCache
from mapproxy.core.config import base_config, abspath
from mapproxy.core.client import auth_data_from_url, HTTPClient

def load_source_loaders():
    source_loaders = {}
    for entry_point in pkg_resources.iter_entry_points('mapproxy.source_loader'):
        source_loaders[entry_point.name] = entry_point
    return source_loaders

source_loaders = load_source_loaders()
del load_source_loaders


def loader(loaders, name):
    """
    Return named class/function from loaders map.
    """
    entry_point = loaders[name]
    module_name, class_name = entry_point.split(':')
    module = __import__(module_name, {}, {}, class_name)
    return getattr(module, class_name)


tile_filter_loaders = {
    'watermark': 'mapproxy.core.tilefilter:WaterMarkTileFilter',
    'pngquant': 'mapproxy.core.tilefilter:PNGQuantTileFilter',
}

def load_tile_filters():
    filters = []
    for key in tile_filter_loaders:
        filters.append(loader(tile_filter_loaders, key))
    filters.sort(key=lambda x: x.priority, reverse=True)
    return filters

tile_filters = load_tile_filters()
del load_tile_filters

server_loaders = {
    'wms': 'mapproxy.wms.conf_loader:create_wms_server',
    'tms': 'mapproxy.tms.conf_loader:create_tms_server',
    'kml': 'mapproxy.kml.conf_loader:create_kml_server',
}
def server_loader(name):
    return loader(server_loaders, name)


from mapproxy.core.grid import TileGrid
from mapproxy.core.request import split_mime_type
from mapproxy.wms.conf_loader import create_request
from mapproxy.core.client import TileClient, TileURLTemplate
from mapproxy.core.source import DebugSource
from mapproxy.core.layer import CacheMapLayer, SRSConditional, ResolutionConditional
from mapproxy.wms.server import WMSServer
from mapproxy.wms.client import WMSClient, WMSInfoClient
from mapproxy.wms.source import WMSSource, WMSInfoSource
from mapproxy.wms.layer import WMSLayer
from mapproxy.tms import TileServer
from mapproxy.tms.layer import TileLayer
from mapproxy.tms.source import TiledSource
from mapproxy.kml import KMLServer

from mapproxy.core.cache import (
    TileManager,
    map_extend_from_grid
)

class ConfigurationError(Exception):
    pass

class ProxyConfiguration(object):
    def __init__(self, conf):
        self.configuration = conf

        self.load_globals()
        self.load_grids()
        self.load_caches()
        self.load_sources()
        self.load_layers()
        self.load_services()
    
    def load_globals(self):
        self.globals = GlobalConfiguration(**self.configuration.get('globals', {}))
    
    def load_grids(self):
        self.grids = {}
        
        self.grids['GLOBAL_GEODETIC'] = GridConfiguration(srs='EPSG:4326', bbox=[-180, -90, 180, 90])
        self.grids['GLOBAL_MERCATOR'] = GridConfiguration(srs='EPSG:900913')
        
        for grid_name, grid_conf in self.configuration.get('grids', {}).iteritems():
            self.grids[grid_name] = GridConfiguration(**grid_conf)
    
    def load_caches(self):
        self.caches = odict()
        caches_conf = self.configuration.get('caches')
        if not caches_conf: return None # TODO config error
        if isinstance(caches_conf, list):
            caches_conf = list_of_dicts_to_ordered_dict(caches_conf)
        for cache_name, cache_conf in caches_conf.iteritems():
            self.caches[cache_name] = CacheConfiguration(name=cache_name, **cache_conf)
    
    def load_sources(self):
        self.sources = {}
        for source_name, source_conf in self.configuration.get('sources', {}).iteritems():
            self.sources[source_name] = SourceConfiguration.load(**source_conf)

    def load_layers(self):
        self.layers = odict()
        layers_conf = self.configuration.get('layers')
        if not layers_conf: return None # TODO config error
        if isinstance(layers_conf, list):
            layers_conf = list_of_dicts_to_ordered_dict(layers_conf)
        for layer_name, layer_conf in layers_conf.iteritems():
            self.layers[layer_name] = LayerConfiguration(name=layer_name, **layer_conf)

    def load_services(self):
        self.services = ServiceConfiguration(**self.configuration.get('services', {}))
        # for service_name, service_conf in self.configuration.get('services', {}).iteritems():
        #     self.services[service_name] = ServiceConfiguration(name=service_name, **service_conf)

def list_of_dicts_to_ordered_dict(dictlist):
    """
    >>> d = list_of_dicts_to_ordered_dict([{'a': 1}, {'b': 2}, {'c': 3}])
    >>> d.items()
    [('a', 1), ('b', 2), ('c', 3)]
    """
    
    result = odict()
    for d in dictlist:
        for k, v in d.iteritems():
            result[k] = v
    return result

class ConfigurationBase(object):
    optional_keys = set()
    required_keys = set()
    defaults = {}
    
    def __init__(self, **kw):
        self.conf = {}
        expected_keys = set(self.optional_keys)
        expected_keys.update(self.required_keys)
        expected_keys.update(self.defaults.keys())
        for k, v in kw.iteritems():
            if k not in expected_keys:
                raise ConfigurationError('unexpected key %s' % k)
            self.conf[k] = v
        
        for k in self.required_keys:
            if k not in self.conf:
                raise ConfigurationError('missing key %s' % k)
        
        for k, v in self.defaults.iteritems():
            if k not in self.conf:
                self.conf[k] = v

class GridConfiguration(ConfigurationBase):
    optional_keys = set('res srs bbox bbox_srs num_levels tile_size base'.split())
    
    def tile_grid(self, context):
        if 'base' in self.conf:
            base_grid_name = self.conf['base']
            conf = context.grids[base_grid_name].conf.copy()
            conf.update(self.conf)
        else:
            conf = self.conf

        srs = SRS(conf['srs'])
        bbox = conf.get('bbox')
        if isinstance(bbox, basestring):
            bbox = [float(x) for x in bbox.split(',')]
        
        if bbox is None and srs == SRS(4326):
            bbox = (-180.0, -90, 180, 90)
        
        if bbox and 'bbox_srs' in conf:
            bbox_srs = SRS(conf['bbox_srs'])
            bbox = bbox_srs.transform_bbox_to(srs, conf['bbox'])
        
        tile_size = conf.get('tile_size')
        if not tile_size:
            tile_size = context.globals.conf['tile_size']
        tile_size = tuple(tile_size)
        
        res = conf.get('res')
        if isinstance(res, list):
            res.sort(reverse=True)
        
        return TileGrid(
            srs=srs,
            tile_size=tile_size,
            res=res,
            bbox=bbox,
            levels=conf.get('num_levels'),
        )

class GlobalConfiguration(ConfigurationBase):
    defaults = {
        'tile_size': [256, 256],
        'meta_size': [4, 4],
        'meta_buffer': 80
    }
    optional_keys = set('image'.split('.'))
    
    def get_value(self, key, local):
        result = dotted_dict_get(key, local)
        if result is None:
            result = dotted_dict_get(key, self.conf)
        
        if result is None:
            result = dotted_dict_get(key, base_config())
            
        return result
    
def dotted_dict_get(key, d):
    """
    >>> dotted_dict_get('foo', {'foo': {'bar': 1}})
    {'bar': 1}
    >>> dotted_dict_get('foo.bar', {'foo': {'bar': 1}})
    1
    >>> dotted_dict_get('bar', {'foo': {'bar': 1}})
    """
    parts = key.split('.')
    try:
        while parts and d:
            d = d[parts.pop(0)]
    except KeyError:
        return None
    return d
    
class SourceConfiguration(ConfigurationBase):
    @classmethod
    def load(cls, **kw):
        source_type = kw['type']
        for subclass in cls.__subclasses__():
            if source_type in subclass.source_type:
                return subclass(**kw)
        
        raise ValueError()

class WMSSourceConfiguration(SourceConfiguration):
    source_type = ('wms',)
    optional_keys = set('''type supported_srs request_format image
        use_direct_from_level wms_opts http'''.split())
    required_keys = set('req'.split())
    
    def http_client(self, context, request):
        http_client = None
        url, (username, password) = auth_data_from_url(request.url)
        if username and password:
            insecure = context.globals.get_value('http.ssl.insecure', self.conf)
            request.url = url
            http_client = HTTPClient(url, username, password, insecure=insecure)
        return http_client
    
    def source(self, context, params):
        
        # TODO params
        request_format = self.conf.get('request_format')
        if request_format:
            params['format'] = request_format
        
        # tile_grid = grid_conf.tile_grid(context)
        
        #TODO legacy
        # params = {'format': 'image/png'} #cache_conf.conf.copy()
        # params['bbox'] = ','.join(str(x) for x in tile_grid.bbox)
        # params['srs'] = tile_grid.srs.srs_code
        
        resampling = context.globals.get_value('image.resampling_method', self.conf)
        
        supported_srs = [SRS(code) for code in self.conf.get('supported_srs', [])]
        version = self.conf.get('wms_opts', {}).get('version', '1.1.1')
        request = create_request(self.conf['req'], params, version=version)
        http_client = self.http_client(context, request)
        client = WMSClient(request, supported_srs, http_client=http_client, 
                           resampling=resampling)
        return WMSSource(client)
    
    def fi_source(self, context, params):
        # tile_grid = grid_conf.tile_grid(context)
        # 
        # params = cache_conf.conf.copy()
        # params['bbox'] = ','.join(str(x) for x in tile_grid.bbox)
        # params['srs'] = tile_grid.srs.srs_code
        supported_srs = [SRS(code) for code in self.conf.get('supported_srs', [])]
        
        fi_source = None
        if self.conf.get('wms_opts', {}).get('featureinfo', False):
            version = self.conf.get('wms_opts', {}).get('version', '1.1.1')
            fi_request = create_request(self.conf['req'], params,
                req_type='featureinfo', version=version)
            fi_client = WMSInfoClient(fi_request, supported_srs=supported_srs)
            fi_source = WMSInfoSource(fi_client)
        return fi_source


class TileSourceConfiguration(SourceConfiguration):
    source_type = ('tiles',)
    optional_keys = set('''type grid request_format origin'''.split())
    required_keys = set('url'.split())
    defaults = {'origin': 'sw', 'grid': 'GLOBAL_MERCATOR'}
    
    def source(self, context, params):
        url = self.conf['url']
        origin = self.conf['origin']
        if origin not in ('sw', 'nw'):
            log.error("ignoring origin '%s', only supports sw and nw")
            origin = 'sw'
            # TODO raise some configuration exception
        
        grid = context.grids[self.conf['grid']].tile_grid(context)
        
        inverse = True if origin == 'nw' else False
        _mime_class, format, _options = split_mime_type(params['format'])
        client = TileClient(TileURLTemplate(url, format=format))
        return TiledSource(grid, client, inverse=inverse)

class DebugSourceConfiguration(SourceConfiguration):
    source_type = ('debug',)
    required_keys = set('type'.split())
    
    def source(self, context, params):
        return DebugSource()

class CacheConfiguration(ConfigurationBase):
    optional_keys = set('''format cache_dir grids link_single_color_images image
        use_direct_from_res use_direct_from_level meta_buffer meta_size'''.split())
    required_keys = set('name sources'.split())
    defaults = {'format': 'image/png', 'grids': ['GLOBAL_MERCATOR']}
    
    @property
    def format(self):
        return self.conf['format'].split('/')[1]
    
    def cache_dir(self, context):
        if 'cache_dir' in self.conf: 
            cache_dir = self.conf['cache_dir']
        else:
            cache_dir = context.configuration.get('global', {}).get('cache', {}).get('base_dir', None)
        
        if not cache_dir:
            cache_dir = base_config().cache.base_dir
        
        return abspath(cache_dir)
        
    def _file_cache(self, grid_conf, context):
        cache_dir = self.cache_dir(context)
        suffix = grid_conf.conf['srs'].replace(':', '')
        cache_dir = os.path.join(cache_dir, self.conf['name'] + '_' + suffix)
        link_single_color_images = self.conf.get('link_single_color_images', False)
        # tile_filter = self.get_tile_filter()
        return FileCache(cache_dir, file_ext=self.format,
            link_single_color_images=link_single_color_images)
    
    def caches(self, context):
        caches = []

        meta_buffer = context.globals.get_value('meta_buffer', self.conf)
        meta_size = context.globals.get_value('meta_size', self.conf)

        for source_conf in [context.sources[s] for s in self.conf['sources']]:
            for grid_conf in [context.grids[g] for g in self.conf['grids']]:
                cache = self._file_cache(grid_conf, context)
                tile_grid = grid_conf.tile_grid(context)
                source = source_conf.source(context, {'format': self.conf['format']})
                mgr = TileManager(tile_grid, cache, [source], self.format,
                                  meta_size=meta_size, meta_buffer=meta_buffer)
                caches.append((tile_grid, mgr))
        return caches
    
    def map_layer(self, context):
        assert len(self.conf['sources']) == 1
        source_conf = context.sources[self.conf['sources'][0]]
        
        resampling = context.globals.get_value('image.resampling_method', self.conf)
        
        caches = []
        main_grid = None
        for grid, tile_manager in self.caches(context):
            if main_grid is None:
                main_grid = grid
            caches.append((CacheMapLayer(tile_manager, resampling=resampling), (grid.srs,)))
        
        if len(caches) == 1:
            layer = caches[0][0]
        else:
            map_extend = map_extend_from_grid(main_grid)
            layer = SRSConditional(caches, map_extend, caches[0][0].transparent)
        
        if 'use_direct_from_level' in self.conf:
            self.conf['use_direct_from_res'] = main_grid.resolution(self.conf['use_direct_from_level'])
        if 'use_direct_from_res' in self.conf:
            layer = ResolutionConditional(layer, source_conf.source(context), self.conf['use_direct_from_res'], main_grid.srs, layer.extend)
        return layer
    
class LayerConfiguration(ConfigurationBase):
    optional_keys = set(''.split())
    required_keys = set('name title sources'.split())
    
    def wms_layer(self, context):
        sources = []
        fi_sources = []
        for source_name in self.conf['sources']:
            fi_source_names = []
            if source_name in context.caches:
                map_layer = context.caches[source_name].map_layer(context)
                fi_source_names = context.caches[source_name].conf['sources']
            elif source_name in context.sources:
                map_layer = context.sources[source_name].source(context, {'format': 'image/jpeg'})
                fi_source_names = [source_name]
            else:
                raise ConfigurationError('source/cache "%s" not found' % source_name)
            sources.append(map_layer)
            
            for fi_source_name in fi_source_names:
                # TODO multiple sources
                if not hasattr(context.sources[fi_source_name], 'fi_source'): continue
                fi_source = context.sources[fi_source_name].fi_source(context, {'format': 'image/jpeg'})
                if fi_source:
                    fi_sources.append(fi_source)
            
        
        layer = WMSLayer({'title': self.conf['title'], 'name': self.conf['name']}, sources, fi_sources)
        return layer
    
    def tile_layers(self, context):
        if len(self.conf['sources']) > 1: return [] #TODO
        
        tile_layers = []
        for cache_name in self.conf['sources']:
            if not cache_name in context.caches: continue
            for grid, cache_source in context.caches[cache_name].caches(context):
                md = {}
                md['title'] = self.conf['title']
                md['name'] = self.conf['name']
                md['name_path'] = (self.conf['name'], grid.srs.srs_code.replace(':', '').upper())
                md['name_internal'] = md['name_path'][0] + '_' + md['name_path'][1]
                md['format'] = context.caches[cache_name].conf['format']
            
                tile_layers.append(TileLayer(md, cache_source))
        
        return tile_layers
        


class ServiceConfiguration(ConfigurationBase):
    optional_keys = set('wms tms kml'.split())
    
    def services(self, context):
        services = {}
        for service_name, service_conf in self.conf.iteritems():
            creator = getattr(self, service_name + '_service', None)
            if not creator:
                raise ValueError('unknown service: %s' % service_name)
            services[service_name] = creator(service_conf or {}, context)
        return services
    
    def kml_service(self, conf, context):
        md = context.services.conf.get('wms', {}).get('md', {}).copy()
        md.update(conf.get('md', {}))
        layers = odict()
        for layer_name, layer_conf in context.layers.iteritems():
            for tile_layer in layer_conf.tile_layers(context):
                if not tile_layer: continue
                layers[tile_layer.md['name_internal']] = tile_layer
        
        return KMLServer(layers, md)
    
    def tms_service(self, conf, context):
        md = context.services.conf.get('wms', {}).get('md', {}).copy()
        md.update(conf.get('md', {}))
        layers = odict()
        for layer_name, layer_conf in context.layers.iteritems():
            for tile_layer in layer_conf.tile_layers(context):
                if not tile_layer: continue
                layers[tile_layer.md['name_internal']] = tile_layer
        
        return TileServer(layers, md)
    
    def wms_service(self, conf, context):
        md = conf.get('md', {})
        attribution = conf.get('attribution')
        layers = odict()
        for layer_name, layer_conf in context.layers.iteritems():
            layers[layer_name] = layer_conf.wms_layer(context)
        return WMSServer(layers, md, attribution=attribution)
    
def load_services(conf_file):
    if hasattr(conf_file, 'read'):
        conf_data = conf_file.read()
    else:
        log.info('Reading services configuration: %s' % conf_file)
        conf_data = open(conf_file).read()
    conf_dict = yaml.load(conf_data)
    conf = ProxyConfiguration(conf_dict)
    
    return conf.services.services(conf)
    
        

