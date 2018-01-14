# -*- coding: utf-8 -*-

import re
import threading
import traceback

from cachetools import func

import pafy

import pykka

import requests

from mopidy_youtube import logger


# decorator for creating async properties using pykka.ThreadingFuture
# A property 'foo' should have a future '_foo'
# On first call we invoke func() which should create the future
# On subsequent calls we just return the future
#
def async_property(func):
    _future_name = '_' + func.__name__

    def wrapper(self):
        if _future_name not in self.__dict__:
            apply(func, (self,))   # should create the future
        return self.__dict__[_future_name]

    return property(wrapper)


# The Video / Playlist classes can be used to load YouTube data. Most data are
# loaded using the (faster) YouTube Data API, while audio_url is loaded via
# pafy (slower). All properties return futures, which gives the possibility to
# load info in the background (using threads), and use it later.
#
# eg
#   video = youtube.Video.get('7uj0hOIm2kY')
#   video.length   # non-blocking, returns future
#   ... later ...
#   print video.length.get()  # blocks until info arrives, if it hasn't already
#
# Entry is a base class of Video and Playlist
#
class Entry(object):
    cache_max_len = 400

    # Use Video.get(id), Playlist.get(id), instead of Video(id), Playlist(id),
    # to fetch a cached object, if available
    #
    @classmethod
    @func.lru_cache(maxsize=cache_max_len)
    def get(cls, id):
        obj = cls()
        obj.id = id
        return obj

    # Search for both videos and playlists using a single API call. Fetches
    # only title, thumbnails, channel (extra queries are needed for length and
    # video_count)
    #
    @classmethod
    def search(cls, q):
        def create_object(item):
            if item['id']['kind'] == 'youtube#video':
                obj = Video.get(item['id']['videoId'])
                obj._set_api_data(['title', 'channel'], item)
            else:
                obj = Playlist.get(item['id']['playlistId'])
                obj._set_api_data(['title', 'channel', 'thumbnails'], item)
            return obj

        data = API.search(q)
        return map(create_object, data['items'])

    # Adds futures for the given fields to all objects in list, unless they
    # already exist. Returns objects for which at least one future was added
    #
    @classmethod
    def _add_futures(cls, list, fields):
        def add(obj):
            added = False
            for k in fields:
                if '_'+k not in obj.__dict__:
                    obj.__dict__['_'+k] = pykka.ThreadingFuture()
                    added = True
            return added

        return filter(add, list)

    # common Video/Playlist properties go to the base class
    #
    @async_property
    def title(self):
        self.load_info([self])

    @async_property
    def channel(self):
        self.load_info([self])

    # sets the given 'fields' of 'self', based on the 'item'
    # data retrieved through the API
    #
    def _set_api_data(self, fields, item):
        for k in fields:
            _k = '_' + k
            future = self.__dict__.get(_k)
            if not future:
                future = self.__dict__[_k] = pykka.ThreadingFuture()

            if not future._queue.empty():  # hack, no public is_set()
                continue

            if not item:
                val = None
            elif k == 'title':
                val = item['snippet']['title']
            elif k == 'channel':
                val = item['snippet']['channelTitle']
            elif k == 'length':
                # convert PT1H2M10S to 3730
                m = re.search('PT((?P<hours>\d+)H)?' +
                              '((?P<minutes>\d+)M)?' +
                              '((?P<seconds>\d+)S)?',
                              item['contentDetails']['duration'])
                val = (int(m.group('hours') or 0) * 3600 +
                       int(m.group('minutes') or 0) * 60 +
                       int(m.group('seconds') or 0))
            elif k == 'video_count':
                val = min(item['contentDetails']['itemCount'], self.max_videos)
            elif k == 'thumbnails':
                val = [
                    val['url']
                    for (key, val) in item['snippet']['thumbnails'].items()
                    if key in ['medium', 'high']
                ]

            future.set(val)


class Video(Entry):

    # loads title, length, channel of multiple videos using one API call for
    # every 50 videos. API calls are split in separate threads.
    #
    @classmethod
    def load_info(cls, list):
        fields = ['title', 'length', 'channel']
        list = cls._add_futures(list, fields)

        def job(sublist):
            try:
                data = API.list_videos([x.id for x in sublist])
                dict = {item['id']: item for item in data['items']}
            except:
                dict = {}

            for video in sublist:
                video._set_api_data(fields, dict.get(video.id))

        # 50 items at a time, make sure order is deterministic so that HTTP
        # requests are replayable in tests
        for i in range(0, len(list), 50):
            sublist = list[i:i+50]
            ThreadPool.run(job, (sublist,))

    @async_property
    def length(self):
        self.load_info([self])

    @async_property
    def thumbnails(self):
        # make it "async" for uniformity with Playlist.thumbnails
        self._thumbnails = pykka.ThreadingFuture()
        self._thumbnails.set([
            'https://i.ytimg.com/vi/%s/%s.jpg' % (self.id, type)
            for type in ['mqdefault', 'hqdefault']
        ])

    # audio_url is the only property retrived using pafy, it's much more
    # expensive than the rest
    #
    @async_property
    def audio_url(self):
        self._audio_url = pykka.ThreadingFuture()

        def job():
            try:
                info = pafy.new(self.id)
            except:
                logger.error('youtube: video "%s" deleted/restricted', self.id)
                self._audio_url.set(None)
                return

            # get aac stream (.m4a) cause gstreamer 0.10 has issues with ogg
            # containing opus format!
            #  test id: cF9z1b5HL7M, playback gives error:
            #   Could not find a audio/x-unknown decoder to handle media.
            #   You might be able to fix this by running: gst-installer
            #   "gstreamer|0.10|mopidy|audio/x-unknown
            #   decoder|decoder-audio/x-unknown, codec-id=(string)A_OPUS"
            #
            uri = info.getbestaudio('m4a', True)
            if not uri:  # get video url
                uri = info.getbest('m4a', True)
            self._audio_url.set(uri.url)

        ThreadPool.run(job)

    @property
    def is_video(self):
        return True


class Playlist(Entry):
    # overridable by config
    max_videos = 60     # max number of videos per playlist

    # loads title, thumbnails, video_count, channel of multiple playlists using
    # one API call for every 50 lists. API calls are split in separate threads.
    #
    @classmethod
    def load_info(cls, list):
        fields = ['title', 'video_count', 'thumbnails', 'channel']
        list = cls._add_futures(list, fields)

        def job(sublist):
            try:
                data = API.list_playlists([x.id for x in sublist])
                dict = {item['id']: item for item in data['items']}
            except:
                dict = {}

            for pl in sublist:
                pl._set_api_data(fields, dict.get(pl.id))

        # 50 items at a time, make sure order is deterministic so that HTTP
        # requests are replayable in tests
        for i in range(0, len(list), 50):
            sublist = list[i:i+50]
            ThreadPool.run(job, (sublist,))

    # loads the list of videos of a playlist using one API call for every 50
    # fetched videos. For every page fetched, Video.load_info is called to
    # start loading video info in a separate thread.
    #
    @async_property
    def videos(self):
        self._videos = pykka.ThreadingFuture()

        def job():
            all_videos = []
            page = ''
            while page is not None and len(all_videos) < self.max_videos:
                try:
                    max_results = min(self.max_videos - len(all_videos), 50)
                    data = API.list_playlistitems(self.id, page, max_results)
                except:
                    break
                page = data.get('nextPageToken') or None

                myvideos = []
                for item in data['items']:
                    video = Video.get(item['snippet']['resourceId']['videoId'])
                    video._set_api_data(['title'], item)
                    myvideos.append(video)
                all_videos += myvideos

                # start loading video info for this batch in the background
                Video.load_info(myvideos)

            self._videos.set(all_videos)

        ThreadPool.run(job)

    @async_property
    def video_count(self):
        self.load_info([self])

    @async_property
    def thumbnails(self):
        self.load_info([self])

    @property
    def is_video(self):
        return False


# Direct access to YouTube Data API
# https://developers.google.com/youtube/v3/docs/
#
class API:
    endpoint = 'https://www.googleapis.com/youtube/v3/'
    session = requests.Session()

    # overridable by config
    search_results = 15
    key = 'AIzaSyAl1Xq9DwdE_KD4AtPaE4EJl3WZe2zCqg4'

    # search for both videos and playlists using a single API call
    # https://developers.google.com/youtube/v3/docs/search
    #
    @classmethod
    def search(cls, q):
        query = {
            'part': 'id,snippet',
            'fields': 'items(id,snippet(title,thumbnails,channelTitle))',
            'maxResults': cls.search_results,
            'type': 'video,playlist',
            'q': q,
            'key': API.key
        }
        result = API.session.get(API.endpoint+'search', params=query)
        return result.json()

    # list videos
    # https://developers.google.com/youtube/v3/docs/videos/list
    @classmethod
    def list_videos(cls, ids):
        query = {
            'part': 'id,snippet,contentDetails',
            'fields': 'items(id,snippet(title,channelTitle),' +
                      'contentDetails(duration))',
            'id': ','.join(ids),
            'key': API.key
        }
        result = API.session.get(API.endpoint+'videos', params=query)
        return result.json()

    # list playlists
    # https://developers.google.com/youtube/v3/docs/playlists/list
    @classmethod
    def list_playlists(cls, ids):
        query = {
            'part': 'id,snippet,contentDetails',
            'fields': 'items(id,snippet(title,thumbnails,channelTitle),' +
                      'contentDetails(itemCount))',
            'id': ','.join(ids),
            'key': API.key
        }
        result = API.session.get(API.endpoint+'playlists', params=query)
        return result.json()

    # list playlist items
    # https://developers.google.com/youtube/v3/docs/playlistItems/list
    @classmethod
    def list_playlistitems(cls, id, page, max_results):
        query = {
            'part': 'id,snippet',
            'fields': 'nextPageToken,' +
                      'items(snippet(title,resourceId(videoId)))',
            'maxResults': max_results,
            'playlistId': id,
            'key': API.key,
            'pageToken': page,
        }
        result = API.session.get(API.endpoint+'playlistItems', params=query)
        return result.json()


# simple 'dynamic' thread pool. Threads are created when new jobs arrive, stay
# active for as long as there are active jobs, and get destroyed afterwards
# (so that there are no long-term threads staying active)
#
class ThreadPool:
    threads_max = 15
    threads_active = 0
    jobs = []
    lock = threading.Lock()     # controls access to threads_active and jobs

    @classmethod
    def worker(cls):
        while True:
            cls.lock.acquire()
            if len(cls.jobs):
                f, args = cls.jobs.pop()
            else:
                # no more jobs, exit thread
                cls.threads_active -= 1
                cls.lock.release()
                break
            cls.lock.release()

            try:
                apply(f, args)
            except Exception as e:
                logger.error('youtube thread error: %s\n%s',
                             e, traceback.format_exc())

    @classmethod
    def run(cls, f, args=()):
        cls.lock.acquire()

        cls.jobs.append((f, args))

        if cls.threads_active < cls.threads_max:
            thread = threading.Thread(target=cls.worker)
            thread.daemon = True
            thread.start()
            cls.threads_active += 1

        cls.lock.release()
