#!/usr/bin/env python
# Software License Agreement (BSD License)
#
# Copyright (c) 2008, Willow Garage, Inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
#  * Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above
#    copyright notice, this list of conditions and the following
#    disclaimer in the documentation and/or other materials provided
#    with the distribution.
#  * Neither the name of Willow Garage, Inc. nor the names of its
#    contributors may be used to endorse or promote products derived
#    from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
# BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
# ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
#
# Revision $Id$

# make sure we aren't using floor division
from __future__ import division, with_statement

import roslib; roslib.load_manifest('rostopic')

NAME='rostopic'

import os
import sys
import cStringIO
import math
import socket
import threading
import time
import traceback
            
import roslib.names
import roslib.scriptutil
import roslib.message 
import rospy
import rosmsg
import rosrecord

from optparse import OptionParser

## don't print string fields in message
_echo_nostr = False
## don't print array fields in message
_echo_noarr = False

class ROSTopicException(Exception):
    """
    Base exception class of rostopic-related errors
    """
    pass
class ROSTopicIOException(ROSTopicException):
    """
    rostopic errors related to network I/O failures
    """
    pass

def succeed(args):
    code, msg, val = args
    if code != 1:
        raise ROSTopicException("remote call failed: %s"%msg)
    return val

def check_master():
    """
    Make sure that master is available
    @raise ROSTopicException: if unable to successfully communicate with master
    """
    try:
        succeed(roslib.scriptutil.get_master().getPid('/'))
    except socket.error:
        raise ROSTopicIOException("Unable to communicate with master!")
    
class ROSTopicHz(object):
    """
    ROSTopicHz receives messages for a topic and computes frequency stats
    """
    def __init__(self, window_size):
        self.lock = threading.Lock()
        self.last_printed_tn = 0
        self.msg_t0 = -1.
        self.msg_tn = 0
        self.times =[]
        
        # can't have infinite window size due to memory restrictions
        if window_size < 0:
            window_size = 50000
        self.window_size = window_size
                
    def callback_hz(self, data):
        """
        ros sub callback
        """
        try:
            curr_rostime = rospy.get_rostime()
            
            self.lock.acquire()

            # time reset
            if curr_rostime.is_zero():
                if len(self.times) > 0:
                    print "time has reset, resetting counters"
                    self.times = []
                return
            
            curr = curr_rostime.to_seconds()
            if self.msg_t0 < 0 or self.msg_t0 > curr:
                #print "reset t0"
                self.msg_t0 = curr
                self.msg_tn = curr
                self.times = []
            else:
                self.times.append(curr - self.msg_tn)
                self.msg_tn = curr

            #only keep statistics for the last 10000 messages so as not to run out of memory
            if len(self.times) > self.window_size - 1:
                self.times.pop(0)
        finally:
            self.lock.release()

    def print_hz(self):
        """
        print the average publishing rate to screen
        """
        if not self.times:
            return
        elif self.msg_tn == self.last_printed_tn:
            print "no new messages"
            return
        try:
            self.lock.acquire()
            #frequency
            
            # The commented-out rate calculate allows the rate to
            # decay when a publisher dies. The uncommented one uses
            # the last received message to perform the calculation.
            # Now that we report a count and keep track of
            # last_printed_tn, it's easier to detect when a
            # publisher dies, so I've gone back to using a non-decaying
            # calculation - kwc
            
            n = len(self.times)
            #rate = (n - 1) / (rospy.get_time() - self.msg_t0)
            mean = sum(self.times) / n
            rate = 1./mean

            #std dev
            std_dev = math.sqrt(sum((x - mean)**2 for x in self.times) /n)

            # min and max
            max_delta = max(self.times)
            min_delta = min(self.times)

            self.last_printed_tn = self.msg_tn
        finally:
            self.lock.release()
        print "average rate: %.3f\n\tmin: %.3fs max: %.3fs std dev: %.5fs window: %s"%(rate, min_delta, max_delta, std_dev, n+1)
    
def rostopic_hz(topic, window_size=-1):
    """
    periodically print the publishing rate of a topic to console until
    shutdown
    @param topic: topic name
    @type  topic: str
    @param window_size: number of messages to average over, -1 for infinite
    @type  window_size: int
    """
    _, real_topic, _ = get_topic_type(topic) #pause hz until topic is published
    if rospy.is_shutdown():
        return
    rospy.init_node(NAME, anonymous=True)
    rt = ROSTopicHz(window_size)
    # we use a large buffer size as we don't know what sort of messages we're dealing with.
    # may parameterize this in the future
    sub = rospy.Subscriber(real_topic, rospy.AnyMsg, rt.callback_hz)
    print "subscribed to [%s]"%real_topic
    while not rospy.is_shutdown():
        time.sleep(1.0)
        rt.print_hz()
    
class ROSTopicBandwidth(object):
    def __init__(self, window_size=100):
        self.lock = threading.Lock()
        self.last_printed_tn = 0
        self.sizes =[]
        self.times =[]        
        self.window_size = window_size or 100
                
    def callback(self, data):
        """ros sub callback"""
        with self.lock:
            try:
                t = time.time()
                self.times.append(t)
                self.sizes.append(len(data._buff)) #AnyMsg instance
                assert(len(self.times) == len(self.sizes))

                if len(self.times) > self.window_size:
                    self.times.pop(0)
                    self.sizes.pop(0)
            except:
                traceback.print_exc()

    def print_bw(self):
        """print the average publishing rate to screen"""
        if len(self.times) < 2:
            return
        with self.lock:
            n = len(self.times)
            tn = time.time()
            t0 = self.times[0]
            
            total = sum(self.sizes)
            bytes_per_s = total / (tn - t0)
            mean = total / n

            #std_dev = math.sqrt(sum((x - mean)**2 for x in self.sizes) /n)

            # min and max
            max_s = max(self.sizes)
            min_s = min(self.sizes)

        #min/max and even mean are likely to be much smaller, but for now I prefer unit consistency
        if bytes_per_s < 1000:
            bw, mean, min_s, max_s = ["%.2fB"%v for v in [bytes_per_s, mean, min_s, max_s]]
        elif bytes_per_s < 1000000:
            bw, mean, min_s, max_s = ["%.2fKB"%(v/1000) for v in [bytes_per_s, mean, min_s, max_s]]  
        else:
            bw, mean, min_s, max_s = ["%.2fMB"%(v/1000000) for v in [bytes_per_s, mean, min_s, max_s]]
            
        print "average: %s/s\n\tmean: %s min: %s max: %s window: %s"%(bw, mean, min_s, max_s, n)

def rostopic_bw(topic, window_size=-1):
    """
    periodically print the received bandwidth of a topic to console until
    shutdown
    """
    check_master()
    _, real_topic, _ = get_topic_type(topic) #pause hz until topic is published
    if rospy.is_shutdown():
        return
    rospy.init_node(NAME, anonymous=True)
    rt = ROSTopicBandwidth(window_size)
    # we use a large buffer size as we don't know what sort of messages we're dealing with.
    # may parameterize this in the future
    sub = rospy.Subscriber(real_topic, rospy.AnyMsg, rt.callback)
    print "subscribed to [%s]"%real_topic
    while not rospy.is_shutdown():
        time.sleep(1.0)
        rt.print_bw()

# TODO: port to the version I wrote for rxplot instead as it should be more efficient

def msgevalgen(pattern):
    """
    generates a function that returns the relevant field (aka 'subtopic') of a Message object
    @param pattern: subtopic, e.g. /x. Must have a leading '/' if specified.
    @type  pattern: str
    @return: function that converts a message into the desired value
    @rtype: fn(rospy.Message) -> value
    """
    if not pattern or pattern == '/':
        return None
    def msgeval(msg):
        # I will probably replace this with some less beautiful but more efficient
        try:
            return eval('msg'+'.'.join(pattern.split('/')))
        except AttributeError, e:
            sys.stdout.write("no field named [%s]"%pattern+"\n")
            return None
    return msgeval
    
def _get_topic_type(topic):
    """
    subroutine for getting the topic type
    @return str, str, fn: topic type, real topic name and fn to evaluate the message instance
    if the topic points to a field within a topic, e.g. /rosout/msg
    """
    try:
        val = succeed(roslib.scriptutil.get_master().getPublishedTopics('/', '/'))
    except socket.error:
        raise ROSTopicIOException("Unable to communicate with master!")

    # exact match first, followed by prefix match
    matches = [(t, t_type) for t, t_type in val if t == topic]
    if not matches:
        matches = [(t, t_type) for t, t_type in val if topic.startswith(t+'/')]
    if matches:
        #TODO logic for multiple matches if we are prefix matching
        t, t_type = matches[0]
        if t_type == roslib.names.ANYTYPE:
            return None, None, None
        if t_type == topic:
            return t_type, None
        return t_type, t, msgevalgen(topic[len(t):])
    else:
        return None, None, None

# NOTE: this is used externally by rxplot
    
def get_topic_type(topic):
    """
    get the topic type
    @return: topic type, real topic name and fn to evaluate the message instance
    if the topic points to a field within a topic, e.g. /rosout/msg
    @rtype str, str, fn
    @raise ROSTopicException: if master cannot be contacted
    """
    topic_type, real_topic, msg_eval = _get_topic_type(topic)
    if topic_type:
        return topic_type, real_topic, msg_eval
    else:
        print >> sys.stderr, "WARNING: topic [%s] does not appear to be published yet"%topic
        while not rospy.is_shutdown():
            topic_type, real_topic, msg_eval = _get_topic_type(topic)
            if topic_type:
                return topic_type, real_topic, msg_eval
            else:
                time.sleep(0.1)
        return None, None, None

def get_topic_class(topic):
    """
    get the topic message class
    @return: message class for topic, real topic
    name, and function for evaluating message objects into the subtopic
    (or None)
    @rtype: roslib.message.Message, str, str
    @raise ROSTopicException: if topic type cannot be determined or loaded
    """
    topic_type, real_topic, msg_eval = get_topic_type(topic)
    if topic_type is None:
        return None, None, None
    msg_class = roslib.message.get_message_class(topic_type)
    if not msg_class:
        raise ROSTopicException("Cannot load message class for [%s]. Are your messages built?"%topic_type)
    return msg_class, real_topic, msg_eval

from itertools import izip

def _str_plot_fields(val, f):
    """
    get CSV representation of fields used by _str_plot
    @return: list of fields as a CSV string
    @rtype: str
    """
    s = _sub_str_plot_fields(val, f)
    if s is not None:
        return "time,"+s

def _sub_str_plot_fields(val, f):
    """recursive helper function for _str_plot_fields"""
    # CSV
    if type(val) in [int, float] or \
           isinstance(val, roslib.rostime.Time) or isinstance(val, roslib.rostime.Duration):
        return f
    elif isinstance(val, rospy.Message):
        sub = [s for s in [_sub_str_plot_fields(getattr(val, a), f+"."+a) for a in val.__slots__] if s]
        if sub:
            return ','.join([s for s in sub])
    elif not _echo_nostr and isinstance(val, basestring):
        return f
    elif not _echo_noarr and type(val) in [list, tuple]:
        if len(val) == 0:
            return None
        val0 = val[0]
        # no arrays of arrays
        if type(val0) in [int, float] or \
               isinstance(val0, rospy.Time) or isinstance(val0, rospy.Duration):
            return ','.join(["%s%s"%(f,x) for x in xrange(0,len(val))])
        elif not _echo_nostr and isinstance(val0, basestring):
            return ','.join(["%s%s"%(f,x) for x in xrange(0,len(val))])
        elif isinstance(val0, rospy.Message):
            labels = ["%s%s"%(f,x) for x in xrange(0,len(val))]
            sub = [s for s in [_sub_str_plot_fields(v, sf) for v,sf in izip(val, labels)] if s]
            if sub:
                return ','.join([s for s in sub])
    return None


def _str_plot(val, time_offset=None):
    """
    convert value to matlab/octave-friendly CSV string representation.
    Reads the state of the _echo_nostrs and _echo_noarr global vars to
    determine which fields are printed.
    @param val: message
    @type  val: Message
    @return: comma-separated list of field values in val
    @rtype: str
    """
    s = _sub_str_plot(val, time_offset)
    if s is not None:
        if time_offset is not None:
            time_offset = time_offset.tons()
        else:
            time_offset = 0            
        if getattr(val, "_has_header", False):
            return "%s,%s"%(val.header.stamp.tons()-time_offset, s)
        else:
            return "%s,%s"%(rospy.get_rostime().tons()-time_offset, s)
    
#TODO: get rid of the ugly use of the _echo_nonostr and _echo_noarr
    
def _sub_str_plot(val, time_offset):
    """Helper routine for _str_plot."""
    # CSV
    if type(val) in [int, float] or \
           isinstance(val, roslib.rostime.Time) or isinstance(val, roslib.rostime.Duration):
        if time_offset is not None and isinstance(val, roslib.rostime.Time):
            return str(val-time_offset)
        else:
            return str(val)    
    elif isinstance(val, rospy.Message):
        sub = [s for s in [_sub_str_plot(getattr(val, a), time_offset) for a in val.__slots__] if s]
        if sub:
            return ','.join([s for s in sub])
    elif not _echo_nostr and isinstance(val, basestring):
        return val
    elif not _echo_noarr and type(val) in [list, tuple]:
        if len(val) == 0:
            return None
        val0 = val[0]
        # no arrays of arrays
        if type(val0) in [int, float] or \
               isinstance(val0, rospy.Time) or isinstance(val0, rospy.Duration):
            return ','.join([str(v) for v in val])
        elif not _echo_nostr and isinstance(val0, basestring):
            return ','.join([v for v in val])            
        elif isinstance(val0, rospy.Message):
            sub = [s for s in [_sub_str_plot(v, time_offset) for v in val] if s]
            if sub:
                return ','.join([s for s in sub])
    return None
        
class CallbackEcho(object):
    """
    Callback instance that can print callback data in a variety of
    formats. Used for all variants of rostopic echo
    """

    def __init__(self, topic, msg_eval, plot=False, filter_fn=None,
                 echo_clear=False, echo_all_topics=False, offset_time=False):
        """
        @param plot: if True, echo in plotting-friendly format
        @type  plot: bool
        @param filter_fn: function that evaluates to True if message is to be echo'd
        @type  filter_fn: fn(topic, msg)
        @param echo_all_topics: (optional) if True, echo all messages in bag
        @type  echo_all_topics: bool
        @param offset_time: (optional) if True, display time as offset from current time
        @type  offset_time: bool
        """
        if topic and topic[-1] == '/':
            topic = topic[:-1]
        self.topic = topic
        self.msg_eval = msg_eval
        self.plot = plot
        self.filter_fn = filter_fn
        self.sep = '---\n' # same as YAML document separator
        self.echo_all_topics = echo_all_topics
        self.offset_time = offset_time

        # determine which strifying function to use
        if plot:
            self.str_fn = _str_plot
            self.sep = ''
        else:
            self.str_fn = roslib.message.strify_message
            if echo_clear:
                self.sep = '\033[2J\033[;H'

        # first tracks whether or not we've printed anything yet. Need this for printing plot fields.
        self.first = True

        # cache
        self.last_topic = None
        self.last_msg_eval = None

    def callback(self, data, topic):
        """
        Callback to pass to rospy.Subscriber or to call
        manually. rospy.Subscriber constructor must also pass in the
        topic name as an additional arg
        @param data: Message
        @type  data: Message    
        @param topic: topic name
        @type  topic: str    
        """
        if self.filter_fn is not None and not self.filter_fn(data):
            return
        try:
            msg_eval = self.msg_eval
            if topic == self.topic:
                pass
            elif self.topic.startswith(topic + '/'):
                # self.topic is actually a reference to topic field, generate msgeval
                if topic == self.last_topic:
                    # use cached eval
                    msg_eval = self.last_msg_eval
                else:
                    # generate msg_eval and cache
                    self.last_msg_eval = msg_eval = msgevalgen(self.topic[len(topic):])
                    self.last_topic = topic
            elif not self.echo_all_topics:
                return

            if msg_eval is not None:
                data = msg_eval(data)
            else:
                val = data
                
            # data can be None if msg_eval returns None
            if data is not None:
                
                # print fields header for plot
                if self.plot and self.first:
                    sys.stdout.write("%"+_str_plot_fields(data, 'field')+'\n')
                    self.first = False

                if self.offset_time:
                    sys.stdout.write(self.sep+self.str_fn(data, time_offset=rospy.get_rostime()) + '\n')
                else:
                    sys.stdout.write(self.sep+self.str_fn(data) + '\n')
                    
            #sys.stdout.flush()
        except IOError:
            rospy.signal_shutdown('IOError')
        except:
            traceback.print_exc()
            
def rostopic_type(topic):
    """
    Print ROS message type of topic to screen
    @param topic: topic name
    @type  topic: str
    """
    print >> sys.stdout, get_topic_type(topic)[0]

def rostopic_echo_bag(callback_echo, bag_file):
    """
    @param topic: topic name
    @type  topic: str
    @param bag_file: name of bag file to echo messages from or None
    @type  bag_file: str
    """
    if not os.path.exists(bag_file):
        raise ROSTopicException("bag file [%s] does not exist"%bag_file)
    first = True
    for t, msg, _ in rosrecord.logplayer(bag_file):
        # bag files can have relative paths in them, this respects any
        # dynamic renaming
        if t[0] != '/':
            t = roslib.scriptutil.script_resolve_name('rostopic', t)
        callback_echo.callback(msg, t)
    
def rostopic_echo(topic, callback_echo, bag_file=None, echo_all_topics=False):
    """
    @param topic: topic name
    @type  topic: str
    @param bag_file str: name of bag file to echo messages from or None
    @type  bag_file: str
    """
    # we have to init a node regardless and bag echoing can print timestamps

    if bag_file:
        # initialize rospy time due to potential timestamp printing
        rospy.rostime.set_rostime_initialized(True)        
        rostopic_echo_bag(callback_echo, bag_file)
    else:
        check_master()
        rospy.init_node(NAME, anonymous=True)
        msg_class, real_topic, msg_eval = get_topic_class(topic)
        if msg_class is None:
            # occurs on ctrl-C
            return
        callback_echo.msg_eval = msg_eval

        sub = rospy.Subscriber(real_topic, msg_class, callback_echo.callback, topic)
        rospy.spin()

_caller_apis = {}
def get_api(master, caller_id):
    caller_api = _caller_apis.get(caller_id, None)
    if not caller_api:
        try:
            code, msg, caller_api = master.lookupNode('/rostopic', caller_id)
        except socket.error:
            raise ROSTopicIOException("Unable to communicate with master!")
        if code != 1:
            caller_api = 'unknown address'%caller_id
        else:
            _caller_apis[caller_id] = caller_api
    return caller_api

def rostopic_list_bag(bag_file, topic=None):
    import rosrecord
    if not os.path.exists(bag_file):
        raise ROSTopicException("bag file [%s] does not exist"%bag_file)
    if topic:
        count = 0
        earliest = None
        latest = None
        for top, msg, t in rosrecord.logplayer(bag_file, raw=True):
            if top == topic:
                count += 1
                if earliest == None:
                    earliest = t
                latest = t
        import time
        earliest, latest = [time.strftime("%d %b %Y %H:%M:%S", time.localtime(t.to_time())) for t in (earliest, latest)]
        print "%s message(s) from %s to %s"%(count, earliest, latest)
    else:
        topics = set()
        for top, msg, _ in rosrecord.logplayer(bag_file, raw=True):
            if top not in topics:
                print top
                topics.add(top)
            if rospy.is_shutdown():
                break

def rostopic_list(topic, verbose=False, subscribers_only=False, publishers_only=False):
    def topic_type(t, pub_topics):
        matches = [t_type for t_name, t_type in pub_topics if t_name == t]
        if matches:
            return matches[0]
        return 'unknown type'

    # #1563
    if subscribers_only and publishers_only:
        raise ROSTopicException("cannot specify both subscribers- and publishers-only")
    
    master = roslib.scriptutil.get_master()
    try:
        state = succeed(master.getSystemState('/rostopic'))

        pubs, subs, _ = state
        publists = [publist for t, publist in pubs if t == topic]
        sublists = [sublist for t, sublist in subs if t == topic]

        pub_topics = succeed(master.getPublishedTopics('/rostopic', '/'))
    except socket.error:
        raise ROSTopicIOException("Unable to communicate with master!")
    
    if topic:
        #print '-'*80
        print "\nType: %s\n"%topic_type(topic, pub_topics)

        import itertools
        if not subscribers_only:
            if publists:
                print "Publishers: "
                for p in itertools.chain(*publists):
                    print " * %s (%s)"%(p, get_api(master, p))
            else:
                print "Publishers: None"
            print ''

        if not publishers_only:
            if sublists:
                print "Subscribers: "
                for p in itertools.chain(*sublists):
                    print " * %s (%s)"%(p, get_api(master, p))
            else:
                print "Subscribers: None"
            print ''
                    
    else:
        #print '-'*80
        if verbose:
            if not subscribers_only:
                print "\nPublished topics:"
                for t, l in pubs:
                    if len(l) > 1:
                        print " * %s [%s] %s publishers"%(t, topic_type(t, pub_topics), len(l))
                    else:
                        print " * %s [%s] 1 publisher"%(t, topic_type(t, pub_topics))                    

            if not publishers_only:
                print ''
                print "Subscribed topics:"
                for t,l in subs:
                    if len(l) > 1:
                        print " * %s [%s] %s subscribers"%(t, topic_type(t, pub_topics), len(l))
                    else:
                        print " * %s [%s] 1 subscriber"%(t, topic_type(t, pub_topics)) 
        else:
            if publishers_only:
                topics = [t for t,_ in pubs]
            elif subscribers_only:
                topics = [t for t,_ in subs]
            else:
                topics = list(set([t for t,_ in pubs] + [t for t,_ in subs]))                
            topics.sort()
            print '\n'.join(topics)

##########################################################################################
# COMMAND PROCESSING #####################################################################
    
def rostopic_cmd_echo():
    def expr_eval(expr):
        def eval_fn(m):
            return eval(expr)
        return eval_fn
    
    args = sys.argv[2:]
    parser = OptionParser(usage="usage: %prog echo [options] /topic", prog=NAME)
    parser.add_option("-b", "--bag",
                      dest="bag", default=None,
                      help="echo messages from .bag file", metavar="BAGFILE")
    parser.add_option("-p", 
                      dest="plot", default=False,
                      action="store_true",
                      help="echo in a plotting friendly format")
    parser.add_option("--filter", 
                      dest="filter_expr", default=None,
                      metavar="FILTER-EXPRESSION",
                      help="Python expression to filter messages that are printed. Expression can use Python builtins as well as m (the message) and topic (the topic name).")
    parser.add_option("--nostr", 
                      dest="nostr", default=False,
                      action="store_true",
                      help="exclude string fields")
    parser.add_option("--noarr",
                      dest="noarr", default=False,
                      action="store_true",
                      help="exclude arrays")
    parser.add_option("-c", "--clear",
                      dest="clear", default=False,
                      action="store_true",
                      help="clear screen before printing next message")
    parser.add_option("-a", "--all",
                      dest="all_topics", default=False,
                      action="store_true",
                      help="display all message in bag, only valid with -b option")
    parser.add_option("--offset",
                      dest="offset_time", default=False,
                      action="store_true",
                      help="display time as offsets from current time (in seconds)")

    (options, args) = parser.parse_args(args)
    if len(args) > 1:
        parser.error("you may only specify one input topic")
    if options.all_topics and not options.bag:
        parser.error("Display all option is only valid when echoing from bag files")
    if options.offset_time and options.bag:
        parser.error("offset time option is not valid with bag files")
    if options.all_topics:
        topic = ''
    else:
        if len(args) == 0:
            parser.error("topic must be specified")        
        topic = roslib.scriptutil.script_resolve_name('rostopic', args[0])
        # suppressing output to keep it clean
        #if not options.plot:
        #    print "rostopic: topic is [%s]"%topic
        
    global _echo_nostr, _echo_noarr
    _echo_nostr = options.nostr
    _echo_noarr = options.noarr

    filter_fn = None
    if options.filter_expr:
        filter_fn = expr_eval(options.filter_expr)

    callback_echo = CallbackEcho(topic, None, plot=options.plot, filter_fn=filter_fn, echo_clear=options.clear, echo_all_topics=options.all_topics, offset_time=options.offset_time)
    try:
        rostopic_echo(topic, callback_echo, bag_file=options.bag)
    except socket.error:
        print >> sys.stderr, "Network communication failed. Most likely failed to communicate with master."
    
def _optparse_topic_only(cmd, argv=sys.argv):
    args = argv[2:]
    parser = OptionParser(usage="usage: %%prog %s /topic"%cmd, prog=NAME)
    (options, args) = parser.parse_args(args)
    if len(args) == 0:
        parser.error("topic must be specified")        
    if len(args) > 1:
        parser.error("you may only specify one input topic")
    return roslib.scriptutil.script_resolve_name('rostopic', args[0])

def rostopic_cmd_type():
    rostopic_type(_optparse_topic_only('type'))
    
def rostopic_cmd_hz(argv=sys.argv):
    args = argv[2:]
    parser = OptionParser(usage="usage: %prog hz /topic", prog=NAME)
    parser.add_option("-w", "--window",
                      dest="window_size", default=-1,
                      help="window size, in # of messages, for calculating rate", metavar="WINDOW")
    (options, args) = parser.parse_args(args)
    if len(args) == 0:
        parser.error("topic must be specified")        
    if len(args) > 1:
        parser.error("you may only specify one input topic")
    try:
        if options.window_size != -1:
            import string
            window_size = string.atoi(options.window_size)
        else:
            window_size = options.window_size
    except:
        parser.error("window size must be an integer")
    topic = roslib.scriptutil.script_resolve_name('rostopic', args[0])
    rostopic_hz(topic, window_size=window_size)

def rostopic_cmd_bw(argv=sys.argv):
    args = argv[2:]
    parser = OptionParser(usage="usage: %prog bw /topic", prog=NAME)
    parser.add_option("-w", "--window",
                      dest="window_size", default=None,
                      help="window size, in # of messages, for calculating rate", metavar="WINDOW")
    options, args = parser.parse_args(args)
    if len(args) == 0:
        parser.error("topic must be specified")        
    if len(args) > 1:
        parser.error("you may only specify one input topic")
    try:
        if options.window_size:
            import string
            window_size = string.atoi(options.window_size)
        else:
            window_size = options.window_size
    except:
        parser.error("window size must be an integer")
    topic = roslib.scriptutil.script_resolve_name('rostopic', args[0])
    rostopic_bw(topic, window_size=window_size)

def rostopic_find(topic_type):
    """
    Lookup topics by topic_type
    @param topic_type: type of topic to find
    @type  topic_type: str
    @return: list of topic names that use topic_type    
    @rtype: [str]
    """
    master = roslib.scriptutil.get_master()
    try:
        t_list = succeed(master.getPublishedTopics('/rostopic', '/'))
    except socket.error:
        raise ROSTopicIOException("Unable to communicate with master!")
    return [t_name for t_name, t_type in t_list if t_type == topic_type]
    
def rostopic_cmd_find(argv=sys.argv):
    """
    Implements 'rostopic type'
    @param argv [str]: command-line args
    @type  argv: command-line args    
    """
    args = argv[2:]
    parser = OptionParser(usage="usage: %prog find msg-type", prog=NAME)
    options, args = parser.parse_args(args)
    if not len(args):
        parser.error("please specify a message type")
    if len(args) > 1:
        parser.error("you may only specify one message type")
    print '\n'.join(rostopic_find(args[0]))
    

def rostopic_pub_init(topic_name, topic_type, rate):
    """
    Create rospy.Publisher instance using specified params. If rate is None, the
    Publisher will be latching-style.
    @param topic_name: name of topic
    @type  topic_name: str
    @param topic_type: name of topic type
    @type  topic_type: str
    @param rate: publishing rate or None to latch value
    @type  rate: int
    @return: topic publisher, message class
    @rtype: rospy.Publisher, rospy.Message.__class__
    """
    topic_name = roslib.scriptutil.script_resolve_name('rostopic', topic_name)
    try:
        msg_class = roslib.message.get_message_class(topic_type)
    except:
        raise ROSTopicException("invalid topic type: %s"%topic_type)
    if msg_class is None:
        raise ROSTopicException("invalid topic type: %s"%topic_type)
    rospy.init_node('rostopic', anonymous=True)
    if rate is None:
        pub = rospy.Publisher(topic_name, msg_class, latch=True)
    else:
        pub = rospy.Publisher(topic_name, msg_class)        
    return pub, msg_class

def _rostopic_pub_rate(pub, msg, rate, verbose=False):
    """
    publish message at specified rate
    @param pub: Publisher instance for topic
    @type  pub: rospy.Publisher
    @param msg: message instance to publish
    @type  msg: Message
    @param rate: publishing rate (hz) or None for just once
    @type  rate: int
    @param verbose: If True, print more verbose output to stdout
    @type  verbose: bool
    """
    try:
        r = rospy.Rate(float(rate))
    except ValueError:
        raise ROSTopicException("Rate must be a number")
    while not rospy.is_shutdown():
        if verbose:
            print "publishing %s"%msg
        pub.publish(msg)
        r.sleep()

_ONCE_DELAY = 3.
def _rostopic_pub_latch(pub, msg, once=False, verbose=False):
    """
    publish and latch message 
    @param pub rospy.Publisher: Publisher instance for topic
    @param msg Message: message instance to publish
    @param once bool: if True, publish message once and then exit after sleep interval
    @param verbose bool: If True, print more verbose output to stdout
    """
    s = "publishing and latching [%s]"%msg if verbose else "publishing and latching message"
    if once:
        s = s + " for %s seconds"%_ONCE_DELAY
    else:
        s = s + ". Press ctrl-C to terminate"
    print s

    try:
        pub.publish(msg)
    except TypeError, e:
        raise ROSTopicException(str(e))

    if once:
        timeout_t = time.time() + _ONCE_DELAY
        while not rospy.is_shutdown() and time.time() < timeout_t:
            rospy.sleep(0.2)
    else:
        rospy.spin()        

def rostopic_pub(pub, msg_class, pub_args, rate=None, once=False, verbose=False):
    """
    @param pub rospy.Publisher: Publisher instance for topic
    @param msg_class Class: Message type
    @param pub_args [val]: arguments to initialize message
    @param rate int: publishing rate (hz) or None for just once
    @param once bool: If True, publish one message and exit. rate must be None
    @param verbose bool: If True, print more verbose output to stdout
    """
    msg = msg_class()
    try:
        roslib.message.fill_message_args(msg, pub_args)
    except roslib.message.ROSMessageException, e:
        raise ROSTopicException(str(e)+"\nArgs are: [%s]"%roslib.message.get_printable_message_args(msg))
    try:
        
        if rate is None:
            _rostopic_pub_latch(pub, msg, once, verbose)
        else:
            _rostopic_pub_rate(pub, msg, rate, verbose)
            
    except rospy.ROSSerializationException, e:
        # we could just print the message definition, but rosmsg is more readable
        raise ROSTopicException("Unable to publish message. One of the fields has an incorrect type:\n"+\
                                "  %s\n\nmsg file:\n%s"%(e, rosmsg.get_msg_text(msg_class._type)))
    
def rostopic_cmd_pub(argv):
    """
    Parse 'pub' command arguments and run command. Will cause a system
    exit if command-line argument parsing fails.
    @param argv: command-line arguments
    @param argv: [str]
    @raise ROSTopicException: if call command cannot be executed
    """
    try:
        import yaml
    except ImportError, e:
        raise ROSTopicException("Cannot import yaml. Please make sure the pyyaml system dependency is installed")

    args = argv[2:]
    parser = OptionParser(usage="usage: %prog pub /topic type [args...]", prog=NAME)
    parser.add_option("-v", dest="verbose", default=False,
                      action="store_true",
                      help="print verbose output")
    parser.add_option("-r", dest="rate", default=None,
                      help="publishing rate (hz)")
    parser.add_option("-1", "--once", action="store_true", dest="once", default=False,
                      help="publish one message and exit")

    (options, args) = parser.parse_args(args)
    if options.rate is not None:
        if options.once:
            parser.error("You cannot select both -r and -1 (--once)")
        try:
            r = float(options.rate)
        except ValueError:
            parser.error("rate must be a number")
        if r <= 0:
            parser.error("rate must be greater than zero")        
        
    if len(args) == 0:
        parser.error("/topic must be specified")
    if len(args) == 1:
        parser.error("topic type must be specified")
    topic_name, topic_type = args[0], args[1]

    # type-case using YAML 
    pub_args = []
    for arg in args[2:]:
        pub_args.append(yaml.load(arg))

    # make sure master is online. we wait until after we've parsed the
    # args to do this so that syntax errors are reported first
    check_master()

    pub, msg_class = rostopic_pub_init(topic_name, topic_type, options.rate)
    if not pub_args and len(msg_class.__slots__):
        # read pub_args from stdin
        for pub_args in _stdin_yaml_arg():
            if rospy.is_shutdown():
                break
            if pub_args:
                rostopic_pub(pub, msg_class, pub_args, options.rate, verbose=options.verbose)
            if rospy.is_shutdown():
                break
    else:
        # if user sent us a dictionary as the only argument, strip the
        # list container as we are mapping it to the entire message
        if len(pub_args) == 1 and type(pub_args[0]) == dict:
            pub_args = pub_args[0]
        rostopic_pub(pub, msg_class, pub_args, options.rate, options.once, verbose=options.verbose)
        
def _stdin_yaml_arg():
    """
    @return: for next yaml document on stdin
    @rtype: iterator
    """
    import yaml
    import select
    poll = select.poll()
    poll.register(sys.stdin, select.POLLIN)
    try:
        arg = 'x'
        while not rospy.is_shutdown() and arg != '\n':
            buff = ''
            while arg != '\n' and arg.strip() != '---':
                val = poll.poll(1.0)
                if not val:
                    continue
                arg = sys.stdin.readline() + '\n'
                if arg.startswith('... logging'):
                    # temporary, until we fix rospy logging
                    continue
                elif arg.strip() != '---':
                    buff = buff + arg
            yield yaml.load(buff.rstrip())
    except select.error:
        return # most likely ctrl-c interrupt
    
def rostopic_cmd_list():
    """command-line parsing for 'rostopic list' command"""
    args = sys.argv[2:]
    parser = OptionParser(usage="usage: %prog list [/topic]", prog=NAME)
    parser.add_option("-b", "--bag",
                      dest="bag", default=None,
                      help="list topics in .bag file", metavar="BAGFILE")
    parser.add_option("-v", "--verbose",
                      dest="verbose", default=False,action="store_true",
                      help="list full details about each topic")
    parser.add_option("-p",
                      dest="publishers", default=False,action="store_true",
                      help="list only publishers")
    parser.add_option("-s",
                      dest="subscribers", default=False,action="store_true",
                      help="list only subscribers")

    (options, args) = parser.parse_args(args)
    topic = None
    if len(args) == 1:
        topic = roslib.scriptutil.script_resolve_name('rostopic', args[0])
    elif len(args) > 1:
        parser.error("you may only specify one input topic")
    if options.bag:
        if options.subscribers: 
            parser.error("-s option is not valid with bags")
        elif options.publishers:
            parser.error("-p option is not valid with bags")            
        rostopic_list_bag(options.bag, topic)
    else:
        if options.subscribers and options.publishers:
            parser.error("you may only specify one of -p, -s")

        rostopic_list(topic, verbose=options.verbose, subscribers_only=options.subscribers, publishers_only=options.publishers)
    
def fullusage():
    print """rostopic is a command-line tool for printing information about ROS Topics.

Commands:
\trostopic bw\tdisplay bandwidth used by topic
\trostopic echo\tprint messages to screen
\trostopic hz\tdisplay publishing rate of topic    
\trostopic list\tprint information about active topics
\trostopic pub\tpublish data to topic
\trostopic type\tprint topic type
\trostopic find\tfind topics by type

Type rostopic <command> -h for more detailed usage, e.g. 'rostopic echo -h'
"""
    sys.exit(os.EX_USAGE)

def rostopicmain(argv=sys.argv):
    # process argv
    if len(argv) == 1:
        fullusage()
    try:
        command = argv[1]
        if command == 'echo':
            rostopic_cmd_echo()
        elif command == 'hz':
            rostopic_cmd_hz(argv)
        elif command == 'type':
            rostopic_cmd_type()
        elif command == 'list':
            rostopic_cmd_list()
        elif command == 'pub':
            rostopic_cmd_pub(argv)
        elif command == 'bw':
            rostopic_cmd_bw(argv)
        elif command == 'find':
            rostopic_cmd_find(argv)
        else:
            fullusage()
    except socket.error:
        print >> sys.stderr, "Network communication failed. Most likely failed to communicate with master."
    except rosrecord.ROSRecordException, e:
        print >> sys.stderr, "ERROR: unable to use bag file: "+str(e)
    except ROSTopicException, e:
        print >> sys.stderr, "ERROR: "+str(e)
    except rospy.ROSInterruptException: pass
        
if __name__ == '__main__':
    rostopicmain(argv=['rostopic', 'hz', 'cloud_pcd'])
