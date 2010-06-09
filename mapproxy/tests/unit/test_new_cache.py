from __future__ import with_statement
import os
import re
import time
import threading
import shutil
import tempfile

from StringIO import StringIO
import Image

from mapproxy.core.cache import (
    FileCache,
    TileManager,
    Source,
    TiledSource,
    WMSSource,
    InvalidSourceQuery,
    Tile,
    CacheMapLayer,
    DirectMapLayer,
    MapQuery,
    WMSClient,
    ResolutionConditional,
    SRSConditional,
    MapExtend,
    TileSourceError,
)
from mapproxy.core.grid import TileGrid
from mapproxy.core.srs import SRS
from mapproxy.core.image import ImageSource

from mapproxy.wms.request import WMS111MapRequest

from mapproxy.tests.image import create_debug_img, is_png, tmp_image
from mapproxy.tests.http import query_eq, mock_httpd

from collections import defaultdict

from nose.tools import eq_, raises, assert_not_equal

TEST_SERVER_ADDRESS = ('127.0.0.1', 56413)
GLOBAL_GEOGRAPHIC_EXTEND = MapExtend((-180, -90, 180, 90), SRS(4326))

tmp_lock_dir = None
def setup():
    global tmp_lock_dir
    tmp_lock_dir = tempfile.mkdtemp()

def teardown():
    shutil.rmtree(tmp_lock_dir)

class counting_set(object):
    def __init__(self, items):
        self.data = defaultdict(int)
        for item in items:
            self.data[item] += 1
    def add(self, item):
        self.data[item] += 1
    
    def __eq__(self, other):
        return self.data == other.data

class MockTileClient(object):
    def __init__(self):
        self.requested_tiles = []
    
    def get_tile(self, tile_coord):
        self.requested_tiles.append(tile_coord)

class TestTiledSourceGlobalGeodetic(object):
    def setup(self):
        self.grid = TileGrid(SRS(4326), bbox=[-180, -90, 180, 90])
        self.client = MockTileClient()
        self.source = TiledSource(self.grid, self.client)
    def test_match(self):
        self.source.get_map(MapQuery([-180, -90, 0, 90], (256, 256), SRS(4326)))
        self.source.get_map(MapQuery([0, -90, 180, 90], (256, 256), SRS(4326)))
        eq_(self.client.requested_tiles, [(0, 0, 1), (1, 0, 1)])
    @raises(InvalidSourceQuery)
    def test_wrong_size(self):
        self.source.get_map(MapQuery([-180, -90, 0, 90], (512, 256), SRS(4326)))
    @raises(InvalidSourceQuery)
    def test_wrong_srs(self):
        self.source.get_map(MapQuery([-180, -90, 0, 90], (512, 256), SRS(4326)))


class TestFileCache(object):
    def setup(self):
        self.cache_dir = tempfile.mkdtemp()
        self.cache = FileCache(cache_dir=self.cache_dir, file_ext='png')
    def teardown(self):
        shutil.rmtree(self.cache_dir)
    
    def test_is_cached_miss(self):
        assert not self.cache.is_cached(Tile((0, 0, 0)))
    
    def test_is_cached_hit(self):
        tile = Tile((0, 0, 0))
        self._create_cached_tile(tile)
        assert self.cache.is_cached(Tile((0, 0, 0)))
    
    def test_is_cached_none(self):
        assert self.cache.is_cached(Tile(None))
    
    def test_load_tile_not_cached(self):
        tile = Tile((0, 0, 0))
        assert self.cache.load(tile) == False
        assert tile.is_missing()
    
    def test_load_tile_cached(self):
        tile = Tile((0, 0, 0))
        self._create_cached_tile(tile)
        assert self.cache.load(tile) == True
        assert not tile.is_missing()
    
    def test_store(self):
        tile = Tile((0, 0, 0), ImageSource(StringIO('foo')))
        self.cache.store(tile)
        assert self.cache.is_cached(tile)
        loc = self.cache.tile_location(tile)
        with open(loc) as f:
            assert f.read() == 'foo'
        assert tile.stored
    
    def test_store_tile_already_stored(self):
        tile = Tile((0, 0, 0), StringIO('foo'))
        tile.stored = True
        self.cache.store(tile)
        loc = self.cache.tile_location(tile)
        assert not os.path.exists(loc)
    
    def test_single_color_tile_store(self):
        img = Image.new('RGB', (256, 256), color='#ff0105')
        tile = Tile((0, 0, 0), ImageSource(img))
        self.cache.link_single_color_images = True
        self.cache.store(tile)
        assert self.cache.is_cached(tile)
        loc = self.cache.tile_location(tile)
        assert os.path.islink(loc)
        assert os.path.realpath(loc).endswith('ff0105.png')
        assert is_png(open(loc, 'rb'))
        
        tile2 = Tile((0, 0, 1), ImageSource(img))
        self.cache.store(tile2)
        assert self.cache.is_cached(tile2)
        loc2 = self.cache.tile_location(tile2)
        assert os.path.islink(loc2)
        assert os.path.realpath(loc2).endswith('ff0105.png')
        assert is_png(open(loc2, 'rb'))
        
        assert_not_equal(loc, loc2)
        assert os.path.samefile(loc, loc2)
    
    def test_single_color_tile_store_w_alpha(self):
        img = Image.new('RGBA', (256, 256), color='#ff0105')
        tile = Tile((0, 0, 0), ImageSource(img))
        self.cache.link_single_color_images = True
        self.cache.store(tile)
        assert self.cache.is_cached(tile)
        loc = self.cache.tile_location(tile)
        assert os.path.islink(loc)
        assert os.path.realpath(loc).endswith('ff0105ff.png')
        assert is_png(open(loc, 'rb'))
    
    def _create_cached_tile(self, tile):
        loc = self.cache.tile_location(tile, create_dir=True)
        with open(loc, 'w') as f:
            f.write('foo')

class MockFileCache(FileCache):
    def __init__(self, *args, **kw):
        FileCache.__init__(self, *args, **kw)
        self.stored_tiles = set()
        self.loaded_tiles = counting_set([])
    
    def store(self, tile):
        assert tile.coord not in self.stored_tiles
        self.stored_tiles.add(tile.coord)
        if self.cache_dir != '/dev/null':
            FileCache.store(self, tile)
    
    def load(self, tile):
        self.loaded_tiles.add(tile.coord)
        return FileCache.load(self, tile)
    
    def is_cached(self, tile):
        return tile.coord in self.stored_tiles
    
class TestTileManagerTiledSource(object):
    def setup(self):
        self.file_cache = MockFileCache('/dev/null', 'png', lock_dir=tmp_lock_dir)
        self.grid = TileGrid(SRS(4326), bbox=[-180, -90, 180, 90])
        self.client = MockTileClient()
        self.source = TiledSource(self.grid, self.client)
        self.tile_mgr = TileManager(self.grid, self.file_cache, [self.source], 'png')
    
    def test_create_tiles(self):
        self.tile_mgr._create_tiles([Tile((0, 0, 1)), Tile((1, 0, 1))])
        eq_(self.file_cache.stored_tiles, set([(0, 0, 1), (1, 0, 1)]))
        eq_(self.client.requested_tiles, [(0, 0, 1), (1, 0, 1)])

class TestTileManagerDifferentSourceGrid(object):
    def setup(self):
        self.file_cache = MockFileCache('/dev/null', 'png', lock_dir=tmp_lock_dir)
        self.grid = TileGrid(SRS(4326), bbox=[-180, -90, 180, 90])
        self.source_grid = TileGrid(SRS(4326), bbox=[0, -90, 180, 90])
        self.client = MockTileClient()
        self.source = TiledSource(self.source_grid, self.client)
        self.tile_mgr = TileManager(self.grid, self.file_cache, [self.source], 'png')
    
    def test_create_tiles(self):
        self.tile_mgr._create_tiles([Tile((1, 0, 1))])
        eq_(self.file_cache.stored_tiles, set([(1, 0, 1)]))
        eq_(self.client.requested_tiles, [(0, 0, 0)])
    
    @raises(InvalidSourceQuery)
    def test_create_tiles_out_of_bounds(self):
        self.tile_mgr._create_tiles([Tile((0, 0, 0))])

class MockSource(Source):
    def __init__(self, *args):
        Source.__init__(self, *args)
        self.requested = []
    
    def get_map(self, query):
        self.requested.append((query.bbox, query.size, query.srs))
        return ImageSource(create_debug_img(query.size))

class TestTileManagerSource(object):
    def setup(self):
        self.file_cache = MockFileCache('/dev/null', 'png', lock_dir=tmp_lock_dir)
        self.grid = TileGrid(SRS(4326), bbox=[-180, -90, 180, 90])
        self.source = MockSource()
        self.tile_mgr = TileManager(self.grid, self.file_cache, [self.source], 'png')
    
    def test_create_tile(self):
        self.tile_mgr._create_tiles([Tile((0, 0, 1)), Tile((1, 0, 1))])
        eq_(self.file_cache.stored_tiles, set([(0, 0, 1), (1, 0, 1)]))
        eq_(self.source.requested,
            [((-180.0, -90.0, 0.0, 90.0), (256, 256), SRS(4326)),
             ((0.0, -90.0, 180.0, 90.0), (256, 256), SRS(4326))])

class MockWMSClient(object):
    def __init__(self):
        self.requested = []
    
    def get_map(self, query):
        self.requested.append((query.bbox, query.size, query.srs))
        return ImageSource(create_debug_img(query.size))

class TestTileManagerWMSSource(object):
    def setup(self):
        self.file_cache = MockFileCache('/dev/null', 'png', lock_dir=tmp_lock_dir)
        self.grid = TileGrid(SRS(4326), bbox=[-180, -90, 180, 90])
        self.client = MockWMSClient()
        self.source = WMSSource(self.client)
        self.tile_mgr = TileManager(self.grid, self.file_cache, [self.source], 'png',
            meta_size=[2, 2], meta_buffer=0)
    
    def test_same_lock_for_meta_tile(self):
        eq_(self.tile_mgr.lock(Tile((0, 0, 1))).lock_file,
            self.tile_mgr.lock(Tile((1, 0, 1))).lock_file
        )
    def test_locks_for_meta_tiles(self):
        assert_not_equal(self.tile_mgr.lock(Tile((0, 0, 2))).lock_file,
                         self.tile_mgr.lock(Tile((2, 0, 2))).lock_file
        )

    def test_create_tile_first_level(self):
        self.tile_mgr._create_tiles([Tile((0, 0, 1)), Tile((1, 0, 1))])
        eq_(self.file_cache.stored_tiles, set([(0, 0, 1), (1, 0, 1)]))
        eq_(self.client.requested,
            [((-180.0, -90.0, 180.0, 90.0), (512, 256), SRS(4326))])
    
    def test_create_tile(self):
        self.tile_mgr._create_tiles([Tile((0, 0, 2))])
        eq_(self.file_cache.stored_tiles,
            set([(0, 0, 2), (1, 0, 2), (0, 1, 2), (1, 1, 2)]))
        eq_(self.client.requested,
            [((-180.0, -90.0, 0.0, 90.0), (512, 512), SRS(4326))])
    
    def test_create_tiles(self):
        self.tile_mgr._create_tiles([Tile((0, 0, 2)), Tile((2, 0, 2))])
        eq_(self.file_cache.stored_tiles,
            set([(0, 0, 2), (1, 0, 2), (0, 1, 2), (1, 1, 2),
                 (2, 0, 2), (3, 0, 2), (2, 1, 2), (3, 1, 2)]))
        eq_(self.client.requested,
            [((-180.0, -90.0, 0.0, 90.0), (512, 512), SRS(4326)),
             ((0.0, -90.0, 180.0, 90.0), (512, 512), SRS(4326))])

    def test_load_tile_coords(self):
        tiles = self.tile_mgr.load_tile_coords(((0, 0, 2), (2, 0, 2)))
        eq_(tiles[0].coord, (0, 0, 2))
        assert isinstance(tiles[0].source, ImageSource)
        eq_(tiles[1].coord, (2, 0, 2))
        assert isinstance(tiles[1].source, ImageSource)
        
        eq_(self.file_cache.stored_tiles,
            set([(0, 0, 2), (1, 0, 2), (0, 1, 2), (1, 1, 2),
                 (2, 0, 2), (3, 0, 2), (2, 1, 2), (3, 1, 2)]))
        eq_(self.client.requested,
            [((-180.0, -90.0, 0.0, 90.0), (512, 512), SRS(4326)),
             ((0.0, -90.0, 180.0, 90.0), (512, 512), SRS(4326))])


class SlowMockSource(MockSource):
    supports_meta_tiles = True
    def get_map(self, query):
        time.sleep(0.1)
        return MockSource.get_map(self, query)

class TestTileManagerLocking(object):
    def setup(self):
        self.tile_dir = tempfile.mkdtemp()
        self.file_cache = MockFileCache(self.tile_dir, 'png', lock_dir=tmp_lock_dir)
        self.grid = TileGrid(SRS(4326), bbox=[-180, -90, 180, 90])
        self.source = SlowMockSource()
        self.tile_mgr = TileManager(self.grid, self.file_cache, [self.source], 'png',
            meta_size=[2, 2], meta_buffer=0)
    
    def test_get_single(self):
        self.tile_mgr._create_tiles([Tile((0, 0, 1)), Tile((1, 0, 1))])
        eq_(self.file_cache.stored_tiles, set([(0, 0, 1), (1, 0, 1)]))
        eq_(self.source.requested,
            [((-180.0, -90.0, 180.0, 90.0), (512, 256), SRS(4326))])
    
    def test_concurrent(self):
        def do_it():
            self.tile_mgr._create_tiles([Tile((0, 0, 1)), Tile((1, 0, 1))])
        
        threads = [threading.Thread(target=do_it) for _ in range(3)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        
        eq_(self.file_cache.stored_tiles, set([(0, 0, 1), (1, 0, 1)]))
        eq_(self.file_cache.loaded_tiles, counting_set([(0, 0, 1), (1, 0, 1), (0, 0, 1), (1, 0, 1)]))
        eq_(self.source.requested,
            [((-180.0, -90.0, 180.0, 90.0), (512, 256), SRS(4326))])
        
        assert os.path.exists(self.file_cache.tile_location(Tile((0, 0, 1))))
    
    def teardown(self):
        shutil.rmtree(self.tile_dir)
    
class TestCacheMapLayer(object):
    def setup(self):
        self.file_cache = MockFileCache('/dev/null', 'png', lock_dir=tmp_lock_dir)
        self.grid = TileGrid(SRS(4326), bbox=[-180, -90, 180, 90])
        self.client = MockWMSClient()
        self.source = WMSSource(self.client)
        self.tile_mgr = TileManager(self.grid, self.file_cache, [self.source], 'png',
            meta_size=[2, 2], meta_buffer=0)
        self.layer = CacheMapLayer(self.tile_mgr)
    
    def test_get_map_small(self):
        result = self.layer.get_map(MapQuery((-180, -90, 180, 90), (300, 150), SRS(4326), 'png'))
        eq_(self.file_cache.stored_tiles, set([(0, 0, 1), (1, 0, 1)]))
        eq_(result.size, (300, 150))
    
    def test_get_map_large(self):
        # gets next resolution layer
        result = self.layer.get_map(MapQuery((-180, -90, 180, 90), (600, 300), SRS(4326), 'png'))
        eq_(self.file_cache.stored_tiles,
            set([(0, 0, 2), (1, 0, 2), (0, 1, 2), (1, 1, 2),
                 (2, 0, 2), (3, 0, 2), (2, 1, 2), (3, 1, 2)]))
        eq_(result.size, (600, 300))
    
    def test_transformed(self):
        result = self.layer.get_map(MapQuery(
            (-20037508.34, -20037508.34, 20037508.34, 20037508.34), (500, 500),
            SRS(900913), 'png'))
        eq_(self.file_cache.stored_tiles,
            set([(0, 0, 2), (1, 0, 2), (0, 1, 2), (1, 1, 2),
                 (2, 0, 2), (3, 0, 2), (2, 1, 2), (3, 1, 2)]))
        eq_(result.size, (500, 500))

class TestDirectMapLayer(object):
    def setup(self):
        self.client = MockWMSClient()
        self.source = WMSSource(self.client)
        self.layer = DirectMapLayer(self.source, GLOBAL_GEOGRAPHIC_EXTEND)
    
    def test_get_map(self):
        result = self.layer.get_map(MapQuery((-180, -90, 180, 90), (300, 150), SRS(4326), 'png'))
        eq_(self.client.requested, [((-180, -90, 180, 90), (300, 150), SRS(4326))])
        eq_(result.size, (300, 150))
    
    def test_get_map_mercator(self):
        result = self.layer.get_map(MapQuery(
            (-20037508.34, -20037508.34, 20037508.34, 20037508.34), (500, 500),
            SRS(900913), 'png'))
        eq_(self.client.requested,
            [((-20037508.34, -20037508.34, 20037508.34, 20037508.34), (500, 500),
              SRS(900913))])
        eq_(result.size, (500, 500))

class TestDirectMapLayerWithSupportedSRS(object):
    def setup(self):
        self.client = MockWMSClient()
        self.source = WMSSource(self.client)
        self.layer = DirectMapLayer(self.source, GLOBAL_GEOGRAPHIC_EXTEND)
    
    def test_get_map(self):
        result = self.layer.get_map(MapQuery((-180, -90, 180, 90), (300, 150), SRS(4326), 'png'))
        eq_(self.client.requested, [((-180, -90, 180, 90), (300, 150), SRS(4326))])
        eq_(result.size, (300, 150))
    
    def test_get_map_mercator(self):
        result = self.layer.get_map(MapQuery(
            (-20037508.34, -20037508.34, 20037508.34, 20037508.34), (500, 500),
            SRS(900913), 'png'))
        eq_(self.client.requested,
            [((-20037508.34, -20037508.34, 20037508.34, 20037508.34), (500, 500),
              SRS(900913))])
        eq_(result.size, (500, 500))


class MockHTTPClient(object):
    def __init__(self):
        self.requested = []
    
    def open(self, url):
        self.requested.append(url)
        w = int(re.search(r'width=(\d+)', url, re.IGNORECASE).group(1))
        h = int(re.search(r'height=(\d+)', url, re.IGNORECASE).group(1))
        format = re.search(r'format=image(/|%2F)(\w+)', url, re.IGNORECASE).group(2)
        result = StringIO()
        create_debug_img((int(w), int(h))).save(result, format=format)
        result.seek(0)
        result.headers = {'Content-type': 'image/'+format}
        return result
    
class TestWMSClient(object):
    def setup(self):
        self.http_client = MockHTTPClient()
        self.req_template = WMS111MapRequest(url='http://localhost/service?', param={
            'format': 'image/png', 'layers': 'foo'
        })
        self.client = WMSClient(self.req_template, http_client=self.http_client,
                                supported_srs=[SRS(4326)])
        
    def test_get_map(self):
        result = self.client.get_map(MapQuery((-180, -90, 180, 90), (300, 150), SRS(4326)))
        assert query_eq(self.http_client.requested[0], "http://localhost/service?"
            "layers=foo&width=300&version=1.1.1&bbox=-180,-90,180,90&service=WMS"
            "&format=image%2Fpng&styles=&srs=EPSG%3A4326&request=GetMap&height=150")
    
    def test_get_map_transformed(self):
        result = self.client.get_map(MapQuery(
           (556597, 4865942, 1669792, 7361866), (300, 150), SRS(900913)))
        assert query_eq(self.http_client.requested[0], "http://localhost/service?"
            "layers=foo&width=300&version=1.1.1"
            "&bbox=4.99999592195,39.9999980766,14.999996749,54.9999994175&service=WMS"
            "&format=image%2Fpng&styles=&srs=EPSG%3A4326&request=GetMap&height=150")

class TestWMSSource(object):
    def setup(self):
        self.req_template = WMS111MapRequest(
            url='http://%s:%d/service?' % TEST_SERVER_ADDRESS,
            param={'format': 'image/png', 'layers': 'foo'})
        self.client = WMSClient(self.req_template)
        self.source = WMSSource(self.client)
    
    def test_get_map(self):
        with tmp_image((512, 512)) as img:
            expected_req = ({'path': r'/service?LAYERS=foo&SERVICE=WMS&FORMAT=image%2Fpng'
                                     '&REQUEST=GetMap&HEIGHT=512&SRS=EPSG%3A4326&styles='
                                     '&VERSION=1.1.1&BBOX=0.0,10.0,10.0,20.0&WIDTH=512'},
                           {'body': img.read(), 'headers': {'content-type': 'image/png'}})
            with mock_httpd(TEST_SERVER_ADDRESS, [expected_req]):
                q = MapQuery((0.0, 10.0, 10.0, 20.0), (512, 512), SRS(4326))
                result = self.source.get_map(q)
                assert isinstance(result, ImageSource)
                eq_(result.size, (512, 512))
                assert is_png(result.as_buffer())
                eq_(result.as_image().size, (512, 512))
    def test_get_map_non_image_content_type(self):
        with tmp_image((512, 512)) as img:
            expected_req = ({'path': r'/service?LAYERS=foo&SERVICE=WMS&FORMAT=image%2Fpng'
                                     '&REQUEST=GetMap&HEIGHT=512&SRS=EPSG%3A4326&styles='
                                     '&VERSION=1.1.1&BBOX=0.0,10.0,10.0,20.0&WIDTH=512'},
                           {'body': 'error', 'headers': {'content-type': 'text/plain'}})
            with mock_httpd(TEST_SERVER_ADDRESS, [expected_req]):
                q = MapQuery((0.0, 10.0, 10.0, 20.0), (512, 512), SRS(4326))
                try:
                    result = self.source.get_map(q)
                except TileSourceError, e:
                    assert 'no image returned' in e.args[0]
                else:
                    assert False, 'no TiledSourceError raised'

class MockLayer(object):
    def __init__(self):
        self.requested = []
    def get_map(self, query):
        self.requested.append((query.bbox, query.size, query.srs))

class TestResolutionConditionalLayers(object):
    def setup(self):
        self.low = MockLayer()
        self.low.transparent = False #TODO
        self.high = MockLayer()
        self.layer = ResolutionConditional(self.low, self.high, 10, SRS(900913),
            GLOBAL_GEOGRAPHIC_EXTEND)
    def test_resolution_low(self):
        self.layer.get_map(MapQuery((0, 0, 10000, 10000), (100, 100), SRS(900913)))
        assert self.low.requested
        assert not self.high.requested
    def test_resolution_high(self):
        self.layer.get_map(MapQuery((0, 0, 100, 100), (100, 100), SRS(900913)))
        assert not self.low.requested
        assert self.high.requested
    def test_resolution_match(self):
        self.layer.get_map(MapQuery((0, 0, 10, 10), (100, 100), SRS(900913)))
        assert not self.low.requested
        assert self.high.requested
    def test_resolution_low_transform(self):
        self.layer.get_map(MapQuery((0, 0, 0.1, 0.1), (100, 100), SRS(4326)))
        assert self.low.requested
        assert not self.high.requested
    def test_resolution_high_transform(self):
        self.layer.get_map(MapQuery((0, 0, 0.005, 0.005), (100, 100), SRS(4326)))
        assert not self.low.requested
        assert self.high.requested

class TestSRSConditionalLayers(object):
    def setup(self):
        self.l4326 = MockLayer()
        self.l900913 = MockLayer()
        self.l32632 = MockLayer()
        self.layer = SRSConditional([
            (self.l4326, (SRS('EPSG:4326'),)), 
            (self.l900913, (SRS('EPSG:900913'), SRS('EPSG:31467'))),
            (self.l32632, (SRSConditional.PROJECTED,)),
        ], GLOBAL_GEOGRAPHIC_EXTEND)
    def test_srs_match(self):
        assert self.layer._select_layer(SRS(4326)) == self.l4326
        assert self.layer._select_layer(SRS(900913)) == self.l900913
        assert self.layer._select_layer(SRS(31467)) == self.l900913
    def test_srs_match_type(self):
        assert self.layer._select_layer(SRS(31466)) == self.l32632
        assert self.layer._select_layer(SRS(32633)) == self.l32632
    def test_no_match_first_type(self):
        assert self.layer._select_layer(SRS(4258)) == self.l4326

class TestNeastedConditionalLayers(object):
    def setup(self):
        self.direct = MockLayer()
        self.l900913 = MockLayer()
        self.l4326 = MockLayer()
        self.layer = ResolutionConditional()
        # TODO