#********************************************************************************
#*                               Dionaea
#*                           - catches bugs -
#*
#*
#*
#* Copyright (c) 2010 Tobias Wulff (twu200 at gmail)
#* 
#* This program is free software; you can redistribute it and/or
#* modify it under the terms of the GNU General Public License
#* as published by the Free Software Foundation; either version 2
#* of the License, or (at your option) any later version.
#* 
#* This program is distributed in the hope that it will be useful,
#* but WITHOUT ANY WARRANTY; without even the implied warranty of
#* MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#* GNU General Public License for more details.
#* 
#* You should have received a copy of the GNU General Public License
#* along with this program; if not, write to the Free Software
#* Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
#* 
#* 
#*             contact nepenthesdev@gmail.com  
#*
#*******************************************************************************/
#*
#*
#* Parts of the SIP response codes and a lot of SIP message parsing are taken
#* from the Twisted Core: http://twistedmatrix.com/trac/wiki/TwistedProjects
#*
#* The hash calculation for SIP authentication has been copied from SIPvicious
#* Sipvicious (c) Sandro Gaucci: http://code.google.com/p/sipvicious
#*******************************************************************************


import logging
import time
import random
import os
import errno
import datetime
import tempfile

from dionaea.core import connection, ihandler, g_dionaea, incident
from dionaea import pyev

from dionaea.sip import rfc3261
from dionaea.sip import rfc4566
from dionaea.sip import rfc2617 # auth
from dionaea.sip.extras import int2bytes, SipConfig


g_default_loop = pyev.default_loop()

logger = logging.getLogger('sip')
logger.setLevel(logging.DEBUG)

_SipSession_sustain_timeout = 20
_SipCall_sustain_timeout = 20

g_sipconfig = SipConfig(g_dionaea.config()['modules']['python'].get("sip", {}))

# SIP headers have short forms
shortHeaders = {"call-id": "i",
                "contact": "m",
                "content-encoding": "e",
                "content-length": "l",
                "content-type": "c",
                "from": "f",
                "subject": "s",
                "to": "t",
                "via": "v",
				"cseq": "cseq",
				"accept": "accept",
				"user-agent": "user-agent",
				"max-forwards": "max-forwards",
				"www-authentication": "www-authentication",
				"authorization": "authorization"
                }

longHeaders = {}
for k, v in shortHeaders.items():
	longHeaders[v] = k
del k, v

class SipParsingError(Exception):
	"""Exception class for errors occuring during SIP message parsing"""

class AuthenticationError(Exception):
	"""Exception class for errors occuring during SIP authentication"""

class SdpParsingError(Exception):
	"""Exception class for errors occuring during SDP message parsing"""

#########
# Classes
#########

class User(object):
	def __init__(self, user_id, branch):
		self.id = user_id
		self.branch = branch
		self.expires = 3600

class RegistrationManager(object):
	def __init__(self):
		self._users = {}
		self._branches = {}

	def register(self, user):
		if not user.id in self._users:
			self._users[user.id] = []

		self._users[user.id].append(user)


reg_manager = RegistrationManager()

class RtpUdpStream(connection):
	"""RTP stream that can send data and writes the whole conversation to a
	file"""
	def __init__(self, localAddress, remoteAddress, port):
		connection.__init__(self, 'udp')

		# Bind to free random port for incoming RTP traffic
		self.bind(localAddress, 0)
		self.connect(remoteAddress, int(port))

		# The address and port of the remote host
		self.remote.host = remoteAddress
		self.remote.port = int(port)

		# Send byte buffer
		self.__sendBuffer = b''

		# Create a stream dump file with date and time and random ID in case of
		# flooding attacks
		global g_sipconfig
		self.__streamDumpIn = None
		dumpDate = time.strftime('%Y-%m-%d')
		dumpTime = time.strftime('%H:%M:%S')
		dumpDir = 'var/dionaea/rtp/{}/'.format(dumpDate)

		# Construct dump file name
		self.__streamDumpFileIn = dumpDir + '{t}_{h}_{p}_in.rtp'.format(
			t=dumpTime, h=self.remote.host, p=self.remote.port)

		# Report incident
		i = incident("dionaea.modules.python.sip.rtp")
		i.con = self
		i.dumpfile = self.__streamDumpFileIn
		i.report()

		logger.info("Created RTP channel on ports :{} <-> :{}".format(
			self.local.port, self.remote.port))

	def close(self):
		if self.__streamDumpIn:
			logger.debug("Closing stream dump (in)")
			self.__streamDumpIn.close()

		connection.close(self)

	def handle_timeout_idle(self):
		return True

	def handle_timeout_sustain(self):
		return True

	def handle_io_in(self, data):
		logger.debug("Incoming RTP data (length {})".format(len(data)))

		if g_sipconfig['record_rtp']:
			# Create stream dump file only if not previously failed
			if not self.__streamDumpIn and self.__streamDumpFileIn:
				self.__startRecording()

			# Write incoming data to disk
			if self.__streamDumpIn:
				self.__streamDumpIn.write(data)

		return len(data)

	def handle_io_out(self):
		logger.debug("Outdoing RTP data (length {})".format(len(data)))

		bytesSent = self.send(self.__sendBuffer)

		# Shift sending window for next send operation
		self.__sendBuffer = self.__sendBuffer[bytesSent:]

	def __startRecording(self):
		dumpDir = self.__streamDumpFileIn.rsplit('/', 1)[0]

		# Create directories if necessary
		try:
			os.mkdir(dumpDir)
		except OSError as e:
			# If directory didn't exist already, rethrow exception
			if e.errno != errno.EEXIST:
				raise e

		# Catch IO errors
		try:
			self.__streamDumpIn = open(self.__streamDumpFileIn, "wb")
		except IOError as e:
			logger.error("RtpStream: Could not open stream dump file: {}".format(e))
			self.__streamDumpIn = None
			self.__streamDumpFileIn = None
		else:
			logger.debug("Created RTP dump file")

class SipCall(connection):
	"""Usually, a new SipSession instance is created when the SIP server
	receives an INVITE message"""
	NO_SESSION, SESSION_SETUP, ACTIVE_SESSION, SESSION_TEARDOWN, INVITE, INVITE_TRYING, INVITE_RINGING, INVITE_CANCEL, CALL = range(9)

	def __init__(self, session, conInfo, rtpPort, invite_message):
		logger.debug("SipCall {} session {} ".format(self, session))
		connection.__init__(self,'udp')
		# Store incoming information of the remote host

		self.__session = session
		self.__state = SipCall.SESSION_SETUP
		self.__remote_address = conInfo[0]
		self.__remote_sip_port = conInfo[1]
		self.__remote_rtp_port = rtpPort
		self.__msg = invite_message
		self.__call_id = invite_message.headers.get(b"call-id").value
		self._rtp_stream = None

		self.local.host = self.__session.local.host
		self.local.port = self.__session.local.port

		self.remote.host = self.__session.remote.host
		self.remote.port = self.__session.remote.port

		user = self.__msg.headers.get(b"to").get_raw().uri.user

		self._user = g_sipconfig.get_user_by_username(
			self.__session.personality,
			user
		)

		# fake a connection entry
		i = incident("dionaea.connection.udp.connect")
		i.con = self
		i.report()

		"""
		# Generate static values for SIP messages
		global g_sipconfig
		self.__sipTo = inviteHeaders['from']
		self.__sipFrom = "{0} <sip:{0}@{1}>".format(
			g_sipconfig['user'], g_sipconfig['domain'])
		self.__sipVia = "SIP/2.0/UDP {}:{}".format(
			g_sipconfig['domain'], g_sipconfig['port'])
		self.__sipContact = "{0} <sip:{0}@{1}>".format(
			g_sipconfig['user'], self.__session.local.host)

		"""
		global _SipCall_sustain_timeout

		# Global timers
		self._timers = {
			#"idle": pyev.Timer(500, 0, g_default_loop, self.__handle_idle_timeout),
			"invite_handler": pyev.Timer(5, 0, g_default_loop, self.__handle_invite),
			#pyev.Timer(3, 0, g_default_loop, self.__handle_invite_timeout),
			#"substain": pyev.Timer(_SipCall_sustain_timeout, 0, g_default_loop, self.__handle_sustain_timeout),
		}

		#self._timers["idle"].start()

	def __handle_invite(self, watcher, events):
		print("---------handle invite")
		if self.__state == SipCall.INVITE:
			print("Send trying")
			# ToDo: Check authentication
			#self.__authenticate(headers)

			if self._user == None:
				msg = self.__msg.create_response(404)
				self.send(msg.dumps())
				self.__state = SipCall.NO_SESSION
				return

			msg = self.__msg.create_response(100)
			"""
			msgLines = []
			msgLines.append("SIP/2.0 " + RESPONSE[RINGING])
			msgLines.append("Via: " + self.__sipVia)
			msgLines.append("Max-Forwards: 70")
			msgLines.append("To: " + self.__sipTo)
			msgLines.append("From: " + self.__sipFrom)
			msgLines.append("Call-ID: {}".format(self.__callId))
			msgLines.append("CSeq: " + headers['cseq'])
			msgLines.append("Contact: " + self.__sipContact)
			msgLines.append("User-Agent: " + g_sipconfig['useragent'])
			"""
			print(msg.dumps())
			self.send(msg.dumps())

			# ToDo: random
			self.__state = SipCall.INVITE_TRYING
			self._timers["invite_handler"].set(1, 0)
			self._timers["invite_handler"].start()
			return

		if self.__state == SipCall.INVITE_TRYING:
			# Send 180 Ringing to make honeypot appear more human-like
			msg = self.__msg.create_response(180)
			"""
			msgLines = []
			msgLines.append("SIP/2.0 " + RESPONSE[RINGING])
			msgLines.append("Via: " + self.__sipVia)
			msgLines.append("Max-Forwards: 70")
			msgLines.append("To: " + self.__sipTo)
			msgLines.append("From: " + self.__sipFrom)
			msgLines.append("Call-ID: {}".format(self.__callId))
			msgLines.append("CSeq: " + headers['cseq'])
			msgLines.append("Contact: " + self.__sipContact)
			msgLines.append("User-Agent: " + g_sipconfig['useragent'])
			"""
			print(msg.dumps())
			self.send(msg.dumps())

			delay = random.randint(self._user.pickup_delay_min, self._user.pickup_delay_max)
			logger.info("Choosing ring delay between {} and {} seconds: {}".format(self._user.pickup_delay_min, self._user.pickup_delay_max, delay))
			self.__state = SipCall.INVITE_RINGING
			self._timers["invite_handler"].set(delay, 0)
			self._timers["invite_handler"].start()
			return

		if self.__state == SipCall.INVITE_RINGING:
			# Create RTP stream instance and pass address and port of listening
			# remote RTP host
			self._rtp_stream = RtpUdpStream(
				self.__session.local.host,
				self.__remote_address,
				self.__remote_rtp_port
			)

			i = incident("dionaea.connection.link")
			i.parent = self
			i.child = self._rtp_stream
			i.report()

			# Send 200 OK and pick up the phone
			msg = self.__msg.create_response(200)
			# ToDo: add IP6 support
			msg.sdp = rfc4566.SDP(
				g_sipconfig.get_sdp_by_name(
					self._user.sdp,
					unicast_address = self.local.host,
					addrtype = "IP4"
				)
			)
			"""
			msgLines = []
			msgLines.append("SIP/2.0 " + RESPONSE[RINGING])
			msgLines.append("Via: " + self.__sipVia)
			msgLines.append("Max-Forwards: 70")
			msgLines.append("To: " + self.__sipTo)
			msgLines.append("From: " + self.__sipFrom)
			msgLines.append("Call-ID: {}".format(self.__callId))
			msgLines.append("CSeq: " + headers['cseq'])
			msgLines.append("Contact: " + self.__sipContact)
			msgLines.append("User-Agent: " + g_sipconfig['useragent'])
			"""
			print(msg.dumps())
			self.send(msg.dumps())

			self.__state = SipCall.CALL

			# ToDo: Send rtp data?
			return


	def __handle_idle_timeout(self, watcher, events):
#		logger.warn("self {} IDLE TIMEOUT watcher {}".format(self, watcher))
		pass

	def __handle_sustain_timeout(self, watcher, events):
		logger.debug("SipCall.__handle_sustain_timeout self {} watcher {}".format(self, watcher))
		self.close()

	def __handle_invite_timeout(self, watcher, events):
		# Send our RTP port to the remote host as a 200 OK response to the
		# remote host's INVITE request
		#logger.debug("SipCall: {} CallID {}".format(self, self.__callId))
		#headers = watcher.data
		#localRtpPort = self._rtpStream.local.port

		msg = self.__msg.create_response(200)

		"""
		msgLines = []
		msgLines.append("SIP/2.0 " + RESPONSE[OK])
		msgLines.append("Via: " + self.__sipVia)
		msgLines.append("Max-Forwards: 70")
		msgLines.append("To: " + self.__sipTo)
		msgLines.append("From: " + self.__sipFrom)
		msgLines.append("Call-ID: {}".format(self.__callId))
		msgLines.append("CSeq: " + headers['cseq'])
		msgLines.append("Contact: " + self.__sipContact)
		msgLines.append("User-Agent: " + g_sipconfig['useragent'])
		msgLines.append("Content-Type: application/sdp")
		msgLines.append("\nv=0")
		msgLines.append("o=... 0 0 IN IP4 localhost")
		msgLines.append("t=0 0")
		msgLines.append("m=audio {} RTP/AVP 0".format(localRtpPort))
		"""

		self.send(msg.dumps())

		# Stop timer
		#self._timers[2].stop()

	def send(self, s):
		self.__session.send(s)

	def close(self):
		logger.debug("SipCall.close {} Session {}".format(self, self.__session))
		# remove Call from Session
		if self.__call_id in self.__session._callids:
			del self.__session._callids[self.__call_id]

		# close rtpStream
		if self._rtp_stream != None:
			self._rtp_stream.close()
			self._rtp_stream = None

		# stop timers
		for name, timer in self._timers.items():
			if timer == None:
				continue

			logger.debug("SipCall timer {} active {} pending {}".format(timer,timer.active,timer.pending))
			if timer.active == True or timer.pending == True:
				logger.warn("SipCall Stopping {}".format(name))
				timer.stop()
		
		# close connection
		connection.close(self)


	def handle_INVITE(self, msg):
		self.__state = SipCall.INVITE
		self._timers["invite_handler"].set(0.1, 0)
		self._timers["invite_handler"].data = msg
		self._timers["invite_handler"].start()
		return 0

	def handle_ACK(self, msg_ack):
		# does this ACK belong to a CANCEL request?
		if not self.__state == SipCall.INVITE_CANCEL:
			logger.info("No method to cancel")
			return

		cseq = self.__msg.headers.get(b"cseq").get_raw()
		cseq_ack = msg_ack.headers.get(b"cseq").get_raw()

		# does this ACK belong to this session?
		if cseq.seq != cseq_ack.seq:
			logger.info("Sequence number doesn't match INVITE id {}; ACK id {}".format(cseq.seq, cseq_ack.seq))
			return

		self.close()
		return True


	def handle_CANCEL(self, msg_cancel):
		# Todo:
		#self.__authenticate(headers)
		if not (self.__state == SipCall.INVITE or self.__state == SipCall.INVITE_TRYING or self.__state == SipCall.INVITE_RINGING):
			logger.info("No method to cancel")
			return

		cseq = self.__msg.headers.get(b"cseq").get_raw()
		cseq_cancel = msg_cancel.headers.get(b"cseq").get_raw()

		if cseq.seq != cseq_cancel.seq:
			logger.info("Sequence number doesn't match INVITE id {}; CANCEL id {}".format(cseq.seq, cseq_cancel.seq))
			return

		self.__state = SipCall.INVITE_CANCEL

		self._timers["invite_handler"].stop()

		# RFC3261 send 487 Request Terminated after cancel
		# old RFC2543 don't send 487
		#ToDo: use timeout to close the session
		msg = self.__msg.create_response(487)
		self.send(msg.dumps())
		msg = msg_cancel.create_response(200)
		self.send(msg.dumps())

	def handle_BYE(self, msg_bye):
		if not self.__state == SipCall.INVITE_CANCEL:
			logger.info("BYE without call")
			return

		msg = msg_bye.create_response(200)
		self.send(msg.dumps())
		self.close()
		return


class SipServer(connection):
	"""Only UDP connections are supported at the moment"""
	def __init__(self):
		connection.__init__(self, 'udp')
		self._sessions = {}
		self._bindings = {}

	def handle_io_in(self, data):
		print(dir(self.local))
		sessionkey = (self.local.host, self.local.port, self.remote.host, self.remote.port)
		print(sessionkey, self.local.hostname)
		if sessionkey not in self._sessions:
			print("new")
			self._sessions[sessionkey] = SipSession(self, sessionkey)

		session = self._sessions[sessionkey]
		logger.debug("{}: {}".format(sessionkey, data))
		session.handle_io_in(data)
		return len(data)


class SipSession(connection):
	def __init__(self, server, sessionkey):
		connection.__init__(self, 'udp')
		# we send everything via the servers connection
		self.server = server
		self.sessionkey = sessionkey
		self.remote.host = server.remote.host
		self.remote.port = server.remote.port
		self.local.host = server.local.host
		self.local.port = server.local.port

		self.personality = g_sipconfig.get_personality_by_address(server.local.host)
		# fake a connection entry
		i = incident("dionaea.connection.udp.connect")
		i.con = self
		i.report()

		# Dictionary with SIP sessions (key is Call-ID)
		self._callids = {}

		logger.info("SIP Session created with personality '{}'".format(self.personality))

		# Setup timers
		global g_default_loop

		global _SipSession_sustain_timeout
		self.timers = [
			pyev.Timer(3, 0, g_default_loop, self.__handle_idle_timeout), # idle
			pyev.Timer(_SipSession_sustain_timeout, 0, g_default_loop, self.__handle_sustain_timeout) # sustain
		]

		# start sustain timer
		self.timers[1].start()

		# we have to create a 'special' bistream for this
		# as all sip traffic shares a single connection
		self.bistream = []

		self._auth = []


	def __handle_idle_timeout(self, watcher, events):
#		logger.warn("self {} SipSession IDLE TIMEOUT watcher".format(self))
		pass

	def __handle_sustain_timeout(self, watcher, events):
		logger.debug("SipSession.__handle_sustain_timeout self {} watcher {}".format(self, watcher))
		self.close()

	def handle_disconnect(self):
		logger.debug("SipSession.handle_disconnect {}".format(self))
		if len(self.bistream) > 0:
			now = datetime.datetime.now()
			dirname = "%04i-%02i-%02i" % (now.year, now.month, now.day)
			dir = os.path.join(g_dionaea.config()['bistreams']['python']['dir'], dirname)
			if not os.path.exists(dir):
				os.makedirs(dir)
			self.fileobj = tempfile.NamedTemporaryFile(delete=False, prefix="Sipsession-" + str(self.local.port) + '-' + self.remote.host + ":" + str(self.remote.port) + "-", dir=dir)
			self.fileobj.write(b"stream = ")
			self.fileobj.write(str(self.bistream).encode())
			self.fileobj.close()
		return False

	def close(self):
		logger.debug("SipSession.close {}".format(self))
		# remove session from server
		if self.sessionkey in self.server._sessions:
			del self.server._sessions[self.sessionkey]

		# close all calls
		for callid in [x for x in self._callids]:
#			logger.debug("closing callid {} call {}".format(callid, self._callids[callid]))
			self._callids[callid].close()
		self._callids = None
		
		# stop timers
		for t in self.timers:
			logger.debug("SipSession timer {} active {} pending {}".format(t,t.active,t.pending))
			if t.active == True or t.pending == True:
#				logger.debug("SipSession Stopping {}".format(t))
				t.stop()

		connection.close(self)

	def send(self, s):
		"""
		The SipSession is not connected, we have to use the origin connection of the server to send.
		"""
		logger.debug('Sending message "{}" to ({}:{})'.format(
			s, self.remote.host, self.remote.port))

		# feed bistream
		self.bistream.append(('out', s))

		# SIP response incident
#		i = incident("dionaea.modules.python.sip.out")
#		i.con = self
#		i.direction = "out"
#		i.msgType = "RESPONSE"
#		i.message = s
#		i.report()
		self.server.send(s, local=(self.local.host,self.local.port),remote=(self.remote.host,self.remote.port))

	def handle_io_in(self, data):

		# feed bistream
		self.bistream.append(('in', data))

		msg = rfc3261.Message(data)

		"""
		# SIP message incident
#		i = incident("dionaea.modules.python.sip.in")
#		i.con = self
#		i.direction = "in"
#		i.msgType = msgType
#		i.firstLine = firstLine
#		i.sipHeaders = headers
#		i.sipBody = body
#		i.report()

		"""

		try:
			func = getattr(self, "handle_" + msg.method.decode("utf-8").upper(), None)
		except:
			func = None

		if func is not None and callable(func) == True:
			func(msg)
		else:
			self.handle_unknown(msg)

		logger.debug("io_in: returning {}".format(len(data)))
		return len(data)

	def handle_unknown(self, msg):
		logger.warn("Unknown SIP header: {}".format(repr(msg.method)))

		res = msg.create_response(501)
		d = res.dumps()
		self.send(res.dumps())


	def handle_ACK(self, msg):
		logger.info("Received ACK")

		#if self.__checkForMissingHeaders(headers):
		#	return

		# Check if session (identified by Call-ID) exists
		# ToDo: check if call-id header exist
		call_id = msg.headers.get(b"call-id").value
		print(self._callids)
		print(self)
		if call_id not in self._callids:
			logger.warn("Given Call-ID does not belong to any session: exit")
			# ToDo: error
			return

		try:
			# Handle incoming ACKs depending on current state
			self._callids[call_id].handle_ACK(msg)
		except AuthenticationError:
			# ToDo error handling
			logger.warn("Authentication failed for ACK request")


	def handle_BYE(self, msg):
		logger.info("Received BYE")

		#if self.__checkForMissingHeaders(headers):
		#	return

		# Check if session (identified by Call-ID) exists
		call_id = msg.headers.get(b"call-id").value
		if call_id not in self._callids:
			logger.warn("Given Call-ID does not belong to any session: exit")
			# ToDo: error
			return

		try:
			# Handle incoming BYE request depending on current state
			self._callids[call_id].handle_BYE(msg)
		except AuthenticationError:
			# ToDo: handle
			logger.warn("Authentication failed for BYE request")


	def handle_CANCEL(self, msg):
		logger.info("Received CANCEL")

		# Check mandatory headers
		#if self.__checkForMissingHeaders(headers):
		#	return

		# Get Call-Id and check if there's already a SipSession
		call_id = msg.headers.get(b"call-id").value

		cseq = msg.headers.get(b"cseq").get_raw()

		#print("-----------------------------cseq", cseq.seq, cseq.method)

		#if cseq.method == b"INVITE" or cseq.method == b"ACK":
		# Find SipSession and delete it
		print("---",call_id, self._callids)
		if call_id not in self._callids:
			logger.warn(
				"CANCEL request does not match any existing SIP session")
			return
		try:
			self._callids[call_id].handle_CANCEL(msg)
		except AuthenticationError:
			logger.warn("Authentication failed for CANCEL request")

		return


	def handle_INVITE(self, msg):
		global g_sipconfig

		# Print SIP header
		#logger.info("Received INVITE")
		#for k, v in headers.items():
	#		logger.debug("SIP header {}: {}".format(k, v))

		# ToDo: content-length? also for udp or only for tcp?
		if not msg.headers_exist([b"content-type"]):
			logger.warn("INVITE without accept and content-type")
			# ToDo: return error
			return

		# Header has to define Content-Type: application/sdp if body contains
		# SDP message. Also, Accept has to be set to sdp so that we can send
		# back a SDP response.
		if msg.headers.get("content-type").value.lower() != b"application/sdp":
			# ToDo: error
			logger.warn("INVITE without SDP message: exit")
			return

		#if msg.headers.get("accept").value.lower() != "application/sdp":
		#	logger.warn("INVITE without SDP message: exit")
			# ToDo: error
		#	return

		if msg.sdp == None:
			logger.warn("INVITE without SDP message: exit")
			# ToDo: error
			return

		# Get RTP port from SDP media description
		medias = msg.sdp[b"m"]
		if len(medias) < 1:
			logger.warn("SDP message has to include a media description: exit")
			# ToDo: error
			return

		audio = None
		for media in medias:
			if media.media.lower() == b"audio":
				audio = media
				# ToDo: parse the rest to find the best one
				break

		if audio == None:
			logger.warn("SDP media description has to be of audio type: exit")
			return

		# Read Call-ID field and create new SipCall instance on first INVITE
		# request received (remote host might send more than one because of time
		# outs or because he wants to flood the honeypot)
		logger.debug("Currently active sessions: {}".format(self._callids))
		call_id = msg.headers.get(b"call-id").value
		print(call_id)
		if call_id in self._callids:
			logger.warn("SIP session with Call-ID {} already exists".format(
				call_id))
			# ToDo: error
			return

		# Establish a new SIP Call
		new_call = SipCall(
			self,
			(self.remote.host, self.remote.port),
			audio.port,
			msg
		)

		# Store session object in sessions dictionary
		self._callids[call_id] = new_call
		print(self._callids)
		print(self)
		i = incident("dionaea.connection.link")
		i.parent = self
		i.child = new_call
		i.report()

		try:
			r = new_call.handle_INVITE(msg)
		except AuthenticationError:
			logger.warn("Authentication failed, not creating SIP session")
			new_call.close()
			del new_call


	def handle_OPTIONS(self, msg):
		logger.info("Received OPTIONS")

		# ToDo: add Contact
		res = msg.create_response(200)
		res.headers.append(rfc3261.Header("INVITE, ACK, CANCEL, OPTIONS, BYE", "Allow"))
		res.headers.append(rfc3261.Header("application/sdp", "Accept"))
		res.headers.append(rfc3261.Header("en", "Accept-Language"))

		self.send(res.dumps())


	def handle_REGISTER(self, msg):
		"""
		:See: http://tools.ietf.org/html/rfc3261#section-10
		"""

		# b"request-uri"
		if not msg.headers_exist([b"to", b"from", b"call-id", b"cseq"]):
			logger.warn("REGISTER, header missing")
			# ToDo: return error
			return

		vias = msg.headers.get(b"via", [])

		via = vias[-1]

		branch = via.get_raw().get_param(b"branch")

		to = msg.headers.get(b"to")
		user_id = to.get_raw().uri.user

		u = g_sipconfig.get_user_by_username(self.personality, user_id)

		if u.password != None:
			header_auth = msg.headers.get(b"authorization", None)
			if header_auth == None or self._auth == None:
				self._auth = rfc2617.Authentication(
					method = "digest",
					algorithm = "md5",
					nonce = "foobar123",
					realm = u.realm
				)
				res = msg.create_response(401)
				res.headers.append(rfc3261.Header(self._auth, b"www-authenticate"))
#				res.headers.append(rfc3261.Header(b"INVITE, ACK, CANCEL, OPTIONS, BYE, REFER, SUBSCRIBE, NOTIFY, INFO", b"Allow"))
				self.send(res.dumps())
				return

			auth_response = rfc2617.Authentication(header_auth[0].value)
			print("-----auth", self._auth)
			if self._auth.check(u.username, "test", "REGISTER", auth_response) == False:
				self._auth = rfc2617.Authentication(
					method = "digest",
					algorithm = "md5",
					nonce = b"foobar123",
					realm = u.realm
				)
				res = msg.create_response(401)
				res.headers.append(rfc3261.Header(self._auth, b"www-authenticate"))
				self.send(res.dumps())
				return

		"""


			# Report authentication incident
			i = incident("dionaea.modules.python.sip.authentication")
			i.authenticationSuccessful = expected == authLineDict['response']
			i.realm = realm
			i.uri = uri
			i.nonce = authLineDict['nonce']
			i.challengeResponse = authLineDict['response']
			i.expected = expected
			i.report()

			if expected != authLineDict['response']:
				sendUnauthorized(authLineDict['nonce'])
				raise AuthenticationError("Authorization failed")

			logger.info("Authorization succeeded")

		"""

		user = User(user_id, branch)
		reg_manager.register(user)

		res = msg.create_response(200)
		self.send(res.dumps())


	def sip_RESPONSE(self, statusLine, headers, body):
		logger.info("Received a response")

