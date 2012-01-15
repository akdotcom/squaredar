#!/usr/bin/python

import foursquare
import logging
#import urllib2
import time
import urllib

try: import simplejson as json
except ImportError: import json

from google.appengine.api import users
from google.appengine.ext import db
from google.appengine.ext import webapp
from google.appengine.ext.webapp.util import run_wsgi_app


            
# prod
config = {'server':'https://foursquare.com',
          'api_server':'https://api.foursquare.com',
          'redirect_uri': 'https://squaredar.appspot.com/oauth',
#          'redirect_uri': 'http://localhost:8088/oauth',
          'client_id': 'CLIENT_ID',
          'client_secret': 'CLIENT_SECRET',
          'push_secret': 'PUSH_SECRET'}

class UserToken(db.Model):
  """Contains the user to foursquare_id + oauth token mapping."""
  user = db.UserProperty()
  fs_id = db.StringProperty()
  token = db.StringProperty()

class Checkin(db.Model):
  """A very simple checkin object, with a denormalized userid for querying."""
  fs_id = db.StringProperty()
  checkin_json = db.TextProperty()

class FriendDistance(db.Model):
  """Information about a friend's distance."""
  fs_id = db.StringProperty()
  friend_fs_id = db.StringProperty()
  last_distance = db.IntegerProperty()
  avg_distance = db.IntegerProperty()
  num_points = db.IntegerProperty()
  last_seen = db.IntegerProperty()
  last_checkin = db.TextProperty()
  def to_dict(self):
    return dict([(p, unicode(getattr(self, p))) for p in self.properties()])

def fetchJson(url):
  """Does a GET to the specified URL and returns a dict representing its reply."""
  logging.info('fetching url: ' + url)
  result = urllib.urlopen(url).read()
  logging.info('got back: ' + result)
  return json.loads(result)

def updateAvg(old_avg, old_num, point):
  """Given an old average of a number of points, updates average with new point"""
  new_total = (old_avg * old_num) + point
  return new_total / (old_num + 1)

def makeFriendDistance(friendDistance, avg_distance, last_distance,
                       last_seen, last_checkin, id=None, friend_id=None,
                       num_points=1):
  if id:
    friendDistance.fs_id = id
  if friend_id:
    friendDistance.friend_fs_id = friend_id
  friendDistance.avg_distance = avg_distance
  friendDistance.last_distance = last_distance
  friendDistance.last_seen = last_seen;
  friendDistance.last_checkin = last_checkin
  friendDistance.num_points = num_points
  return friendDistance


def makeFoursquareClient(config, access_token=None):
  return foursquare.Foursquare(client_id=config['client_id'],
                               client_secret=config['client_secret'],
                               redirect_uri=config['redirect_uri'],
                               access_token=access_token)

class OAuth(webapp.RequestHandler):
  """Handle the OAuth redirect back to the service."""
  def post(self):
    self.get()

  def get(self):
    code = self.request.get('code')
    logging.info('code ' + code)
    
    client = makeFoursquareClient(config)
    access_token = client.oauth.get_token(code)
    logging.info('token ' + access_token)

    token = UserToken()
#    token.token = json['access_token']
    token.token = access_token
    token.user = users.get_current_user()

    self_response = fetchJson('%s/v2/users/self?oauth_token=%s' % (config['api_server'], token.token))

    token.fs_id = self_response['response']['user']['id']
    token.put()

    self.redirect("/")

class ReceiveCheckin(webapp.RequestHandler):
  """Received a pushed checkin and store it in the datastore."""
  def post(self):
    checkin_json = json.loads(self.request.get('checkin'))
    user_json = checkin_json['user']
    checkin = Checkin()
    checkin.fs_id = user_json['id']
    checkin.checkin_json = json.dumps(checkin_json)
    checkin.put()
    fs_id = checkin.fs_id
    lat = str(checkin_json['venue']['location']['lat'])
    lng = str(checkin_json['venue']['location']['lng'])
    latlng = lat+','+lng
    user = UserToken.all().filter("fs_id = ", fs_id).get()
    if user:
      client = makeFoursquareClient(config, access_token=user.token)
      recent_checkins = client.checkins.recent(params={'ll': latlng})
      distance_map = {}
      checkin_map = {}
      for checkin in recent_checkins['recent']:
        user_id = str(checkin['user']['id'])
        distance_map[user_id] = checkin['distance']
        checkin_map[user_id] = json.dumps(checkin)
      logging.info(distance_map)
      # Parallelize all of this with TaskQueues
      friendDistances = FriendDistance.all().filter("fs_id = ", fs_id)

      missed_friend_ids = distance_map.keys()
      for friend in friendDistances:
        if friend.friend_fs_id in distance_map:
          missed_friend_ids.remove(friend.friend_fs_id)
          distance = distance_map[friend.friend_fs_id]
          avg_distance = friend.avg_distance
          updated_avg_distance = updateAvg(avg_distance,
                                          friend.num_points,
                                          distance)
          makeFriendDistance(friendDistance=friend,
                             avg_distance=updated_avg_distance,
                             last_distance=distance,
                             last_seen=int(time.time()),
                             last_checkin=checkin_map[friend.friend_fs_id],
                             num_points=(friend.num_points + 1))

#          friend.avg_distance = updateAvg(avg_distance,
#                                          friend.num_points,
#                                          distance)
#          friend.last_distance = distance
#          friend.last_seen = int(time.time());
#          friend.last_checkin = checkin_map[friend.friend_fs_id]
#          friend.num_points += 1
          friend.put() # Bulk action? or perhaps parallelized
          logging.info('Updated distance record for ' + friend.friend_fs_id)
      for missed_friend_id in missed_friend_ids:
        friend = FriendDistance()
        makeFriendDistance(friendDistance=friend,
                           id=fs_id,
                           friend_id=missed_friend_id,
                           avg_distance=distance_map[missed_friend_id],
                           last_distance=distance_map[missed_friend_id],
                           last_seen=int(time.time()),
                           last_checkin=checkin_map[missed_friend_id],
                           num_points=1)
        friend.put() # Bulk action? or perhaps parallelized
        logging.info('Created new record for ' + missed_friend_id)
    else:
      logging.info('ruh-roh')
    
    

class FetchCheckins(webapp.RequestHandler):
  """Fetch the checkins we've received via push for the current user."""
  def get(self):
    user = UserToken.all().filter("user = ", users.get_current_user()).get()
    ret = []
    if user:
      checkins = Checkin.all().filter("fs_id = ", user.fs_id).fetch(1000)
      ret = [c.checkin_json for c in checkins]
    self.response.out.write('['+ (','.join(ret)) +']')

class FetchDistances(webapp.RequestHandler):
  """Fetch the checkins we've received via push for the current user."""
  def get(self):
    user = UserToken.all().filter("user = ", users.get_current_user()).get()
    ret = []
    if user:
      distances = FriendDistance.all().filter("fs_id = ", user.fs_id).fetch(1000)
      ret = [json.dumps(d.to_dict()) for d in distances]
    self.response.out.write('['+ (','.join(ret)) +']')


class GetConfig(webapp.RequestHandler):
  """Returns the OAuth URI as JSON so the constants aren't in two places."""
  def get(self):
    uri = '%(server)s/oauth2/authenticate?client_id=%(client_id)s&response_type=code&redirect_uri=%(redirect_uri)s' % config
    self.response.out.write(json.dumps({'auth_uri': uri}))

application = webapp.WSGIApplication([('/oauth', OAuth), 
                                      ('/checkin', ReceiveCheckin),
                                      ('/distances', FetchDistances),
                                      ('/fetch', FetchCheckins),
                                      ('/config', GetConfig)],
                                     debug=True)

def main():
  run_wsgi_app(application)

if __name__ == "__main__":
  main()
