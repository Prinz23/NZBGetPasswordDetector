#!/usr/bin/env python
#
# Queue-script for NZBGet
#
# Copyright (C) 2014-2015 Andrey Prygunkov <hugbug@users.sourceforge.net>
# Copyright (C) 2014 Clinton Hall <clintonhall@users.sourceforge.net>
# Copyright (C) 2014 JVM <jvmed@users.sourceforge.net>
# Copyright (C) 2014 get39678
# Copyright (C) 2014 prinz2311
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
# 
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
#

##############################################################################
### NZBGET QUEUE/POST-PROCESSING SCRIPT                                    ###
### QUEUE EVENTS: NZB_ADDED, NZB_DOWNLOADED, FILE_DOWNLOADED

# Detect nzbs with password protected .rar archive.
#
# If a password is detected the download is marked as bad or paused. This status
# informs other scripts about failure and allows NZBGet to choose
# another duplicate for download (if available).
#
# Further discussion and updates at: 
# http://nzbget.net/forum/viewtopic.php?f=8&t=1391
#
#
# PP-Script version: 1.8.
#
# NOTE: This script requires Python to be installed on your system.
#
##############################################################################
### OPTIONS                                                                ###

# Action if password found (Pause, Mark Bad).
#
# Pause allows to define password. Once resumed, password detection skipped to 
# allow NZB to complete.
#
# Mark bad removes the download from queue and (if option "DeleteCleanupDisk" is active) the
# downloaded files are deleted from disk. If duplicate handling is active
# (option "DupeCheck") then another duplicate is chosen for download
# if available. The status "FAILURE/BAD" is passed to other scripts and informs them
# about failure.
#
#PassAction=Pause

### NZBGET QUEUE/POST-PROCESSING SCRIPT                                    ###
##############################################################################
from __future__ import absolute_import

import os
import sys
import subprocess
import re
import requests
import traceback

PY2 = 2 == sys.version_info[0]
PY3 = 3 == sys.version_info[0]

if PY3:
	text_type = str
	binary_type = bytes

else:
	text_type = unicode
	binary_type = str

from xmlrpclib_to import ServerProxy
import shlex

# Exit codes used by NZBGet
POSTPROCESS_SUCCESS = 93
POSTPROCESS_NONE = 95
POSTPROCESS_ERROR = 94

### Hidden options, should not need to be changed

# Verbose logging for debugging (True, False)
verbose = False
# Unrar Parameters used to obtain unrar output (line 215)
# Strings to check if rar is password protected (comma separated list)
PasswordStrings = '*,wrong password,The specified password is incorrect,encrypted headers,Incorrect password for'


def ensure_str(s, encoding='utf-8', errors='strict'):
	"""Coerce *s* to `str`.

	For Python 2:
	  - `unicode` -> encoded to `str`
	  - `str` -> `str`

	For Python 3:
	  - `str` -> `str`
	  - `bytes` -> decoded to `str`
	"""
	# Optimization: Fast return for the common case.
	if type(s) is str:
		return s
	if PY2 and isinstance(s, text_type):
		return s.encode(encoding, errors)
	elif PY3 and isinstance(s, binary_type):
		return s.decode(encoding, errors)
	elif not isinstance(s, (text_type, binary_type)):
		raise TypeError("not expecting type '%s'" % type(s))
	return s


# Start up checks
def start_check():
	# Check if the script is called from a compatible NZBGet version (as queue-script or as pp-script)
	if not ('NZBNA_EVENT' in os.environ or 'NZBPP_DIRECTORY' in os.environ) or 'NZBOP_ARTICLECACHE' not in os.environ:
		print('*** NZBGet queue script ***')
		print('This script is supposed to be called from nzbget (14.0 or later).')
		sys.exit(1)

	# This script processes only certain queue events.
	# For compatibility with newer NZBGet versions it ignores event types it doesn't know
	if os.environ.get('NZBNA_EVENT') not in ['NZB_ADDED', 'FILE_DOWNLOADED', 'NZB_DOWNLOADED', None]:
		sys.exit(0)

	# If nzb was already marked as bad don't do any further detection
	if os.environ.get('NZBPP_STATUS') == 'FAILURE/BAD':
		if os.environ.get('NZBPR_PASSWORDDETECTOR_HASPASSWORD') == 'yes':
			# Print the message again during post-processing to add it into the post-processing log
			# (which is then can be used by notification scripts such as EMail.py)
			print('[WARNING] Download is password protected')
		clean_up()
		sys.exit(POSTPROCESS_SUCCESS)

	# Check if password previously found
	if os.environ.get('NZBPR_PASSWORDDETECTOR_HASPASSWORD') == 'yes':
		print('[DETAIL] Password previously found, skipping detection')
		if 'NZBPP_DIRECTORY' in os.environ:
			clean_up()  # PProcessing, clean_up
		sys.exit(POSTPROCESS_SUCCESS)

	# Check if a previous scan script or user via web ui has defined a password
	if 'NZBPR_*Unpack:Password' in os.environ:
		print('[DETAIL] Password previously defined, skipping detection')
		if 'NZBPP_DIRECTORY' in os.environ:
			clean_up()  # PProcessing, clean_up
		sys.exit(POSTPROCESS_SUCCESS)
		
	# If called via "Post-process again" from history details dialog the download may not exist anymore
	if 'NZBPP_DIRECTORY' in os.environ and not os.path.exists(os.environ.get('NZBPP_DIRECTORY')):
		print('Destination directory doesn\'t exist, exiting')
		clean_up()
		sys.exit(POSTPROCESS_NONE)

	# If nzb is already failed, don't do any further detection
	if os.environ.get('NZBPP_TOTALSTATUS') == 'FAILURE':
		clean_up()
		sys.exit(POSTPROCESS_NONE)
	
	# Check settings
	optname = 'NZBPO_PASSACTION'
	if optname not in os.environ:
		print('[ERROR] Option %s is missing in configuration file. Please check script settings' % optname[6:])
		sys.exit(POSTPROCESS_ERROR)


# Check if the verbose logging option is enabled
def check_verbose_logging():
	if verbose:
		return True
	return False


# Check the "PASSWORDSTRINGS" against archive output
def check_passwordstrings(outtext, errtext):
	if check_verbose_logging():
		if len(outtext) > 0:
			print("out: " + outtext.translate(None, '\r\n'))
		if len(errtext) > 0:
			print("error: " + errtext.translate(None, '\r\n'))

	PasswordString = PasswordStrings.split(',')
	
	# must not be blank
	if PasswordString == [] or PasswordString == ['']:
		return False

	for m_string in PasswordString:
		m_string = m_string.strip().lower()
		if m_string != '':
			if m_string in outtext.lower():
				return True
			if m_string in errtext.lower():
				return True
	return False


# Finds untested files, comparing all files and processed files in tmp_file
def get_latest_file(dir_name):
	try:
		with open(tmp_file_name) as tmp_file:
			tested = tmp_file.read().splitlines()
			files = os.listdir(dir_name)
			return list(set(files)-set(tested))
	except:
		# tmp_file doesn't exist, all files need testing
		temp_folder = os.path.dirname(tmp_file_name)
		if not os.path.exists(temp_folder):
			os.makedirs(temp_folder)
			print('[DETAIL] Created folder ' + temp_folder)
		with open(tmp_file_name, "w") as tmp_file:
			tmp_file.write('')
			print('[DETAIL] Created temp file ' + tmp_file_name)
		return os.listdir(dir_name)


# Saves tested files so to not test again
def save_tested(data):
	with open(tmp_file_name, "a") as tmp_file:
		tmp_file.write(data)
		
# Extract path to unrar from NZBGet's global option "UnrarCmd";
# Since v15 "UnrarCmd" may contain extra parameters passed to unrar;
# We have to strip these parameters because we need only the path to unrar.
# Returns path to unrar executable.


def unrar():
	exe_name = 'unrar.exe' if os.name == 'nt' else 'unrar'
	UnrarCmd = os.environ['NZBOP_UNRARCMD']
	if os.path.isfile(UnrarCmd) and UnrarCmd.lower().endswith(exe_name):
		return UnrarCmd
	args = shlex.split(UnrarCmd)
	for arg in args:
		if arg.lower().endswith(exe_name):
			return arg
	# We were unable to determine the path to unrar;
	# Let's use the exe name with a hope it's in the search path
	return exe_name
	

# Checks files for passwords without unpacking
def contains_password(dir_name):
	files = get_latest_file(dir_name)
	tested = ''
	for file in files:
		# avoid .tmp files as corrupt
		if "tmp" not in file:
			try:
				command = [unrar(), 'l', '-p-', '-c-', dir_name + '/' + file]
				if check_verbose_logging():
					print('command: %s' % command)
				proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
				out, err = proc.communicate()
				out, err = ensure_str(out), ensure_str(err)
				if check_passwordstrings(out, err):
					return True
			except Exception as e:
				print('[ERROR] Failed %s: %s' % (file, e))
				if verbose:
					traceback.print_exc() 
		tested += file + '\n'
	save_tested(tested)


# Pause NZB group by API
def pause_nzb(NZBID):
	# Setup connection to NZBGet RPC-server
	nzbget = connect_to_nzbget()
	# Pause nzb
	nzbget.editqueue('GroupPause', 0, '', [int(NZBID)])


# Establish connection to NZBGet via RPC-API
def connect_to_nzbget():
	# First we need to know connection info: host, port and password of NZBGet server.
	# NZBGet passes all configuration options to scripts as environment variables.
	host = os.environ['NZBOP_CONTROLIP']
	if host == '0.0.0.0':
		host = '127.0.0.1'
	port = os.environ['NZBOP_CONTROLPORT']
	username = os.environ['NZBOP_CONTROLUSERNAME']
	password = os.environ['NZBOP_CONTROLPASSWORD']
	
	# Build an URL for XML-RPC requests
	# TODO: encode username and password in URL-format
	xmlRpcUrl = 'http://%s:%s@%s:%s/xmlrpc' % (username, password, host, port)
	
	# Create remote server object
	nzbget = ServerProxy(xmlRpcUrl, timeout=300)
	return nzbget


# Connect to NZBGet and call an RPC-API-method without using of python's XML-RPC.
# XML-RPC is easy to use but it is slow for large amount of data
def call_nzbget_direct(url_command):
	# First we need to know connection info: host, port and password of NZBGet server.
	# NZBGet passes all configuration options to scripts as environment variables.
	host = os.environ['NZBOP_CONTROLIP']
	if host == '0.0.0.0':
		host = '127.0.0.1'
	port = os.environ['NZBOP_CONTROLPORT']
	username = os.environ['NZBOP_CONTROLUSERNAME']
	password = os.environ['NZBOP_CONTROLPASSWORD']
	url = 'http://%s:%s/jsonrpc/%s' % ((host, '127.0.0.1')['0.0.0.0' == host], port, url_command)

	try:
		response = requests.get(url, auth=(username, password))
	except requests.RequestException:
		return ''

	return ensure_str(response.content) if response.ok else ''


# Reorder inner files for earlier fake detection
def sort_inner_files():
	nzb_id = int(os.environ.get('NZBNA_NZBID'))

	# Building command-URL to call method "listfiles" passing three parameters: (0, 0, nzb_id)
	url_command = 'listfiles?1=0&2=0&3=%i' % nzb_id
	data = call_nzbget_direct(url_command)
	
	# The "data" is a raw json-string. We could use json.loads(data) to
	# parse it but json-module is slow. We parse it on our own.

	# Iterate through the list of files to find the last rar-file.
	# The last is the one with the highest XX in ".partXX.rar" or ".rXX"
	regex1 = re.compile(r'.*\.part(\d+)\.rar', re.IGNORECASE)
	regex2 = re.compile(r'.*\.r(\d+)', re.IGNORECASE)
	file_num = None
	file_id = None
	file_name = None
	
	for line in data.splitlines():
		if line.startswith('"ID" : '):
			cur_id = int(line[7:len(line)-1])
		if line.startswith('"Filename" : "'):
			cur_name = line[14:len(line)-2]
			match = regex1.match(cur_name) or regex2.match(cur_name)
			if match:
				cur_num = int(match.group(1))
				if not file_num or cur_num > file_num:
					file_num = cur_num
					file_id = cur_id
					file_name = cur_name

	# Move the last rar-file to the top of file list
	if file_id:
		print('[INFO] Moving last rar-file to the top: %s' % file_name)
		# Create remote server object
		nzbget = connect_to_nzbget()
		# Using RPC-method "editqueue" of XML-RPC-object "nzbget".
		# we could use direct http access here too but the speed isn't
		# an issue here and XML-RPC is easier to use.
		nzbget.editqueue('FileMoveTop', 0, '', [file_id])
	else:
		print('[INFO] Skipping sorting since could not find any rar-files')


# Remove current and any old temp files
def clean_up():
	nzb_id = os.environ.get('NZBPP_NZBID')
	temp_folder = os.environ.get('NZBOP_TEMPDIR') + '/PasswordDetector'

	nzbids = []
	if os.path.isdir(temp_folder):
		files = os.listdir(temp_folder)

		if len(files) > 1:
			# Create the list of nzbs in download queue
			data = call_nzbget_direct('listgroups?1=0')
			# The "data" is a raw json-string. We could use json.loads(data) to
			# parse it but json-module is slow. We parse it on our own.
			for line in data.splitlines():
				if line.startswith('"NZBID" : '):
					cur_id = int(line[10:len(line)-1])
					nzbids.append(str(cur_id))

		old_temp_files = list(set(files)-set(nzbids))
		if nzb_id in files and nzb_id not in old_temp_files:
			old_temp_files.append(nzb_id)

		for temp_id in old_temp_files:
			temp_file = temp_folder + '/' + str(temp_id)
			try:
				print('[DETAIL] Removing temp file ' + temp_file)
				os.remove(temp_file)
			except:
				print('[ERROR] Could not remove temp file ' + temp_file)


# Script body
def main():
	# Globally define directory for storing list of tested files
	global tmp_file_name

	# Do start up check
	start_check()
	
	# That's how we determine if the download is still runnning or is completely downloaded.
	# We don't use this info in the fake detector (yet).
	Downloading = os.environ.get('NZBNA_EVENT') == 'FILE_DOWNLOADED'
	
	# Depending on the mode in which the script was called (queue-script
	# or post-processing-script) a different set of parameters (env. vars)
	# is passed. They also have different prefixes:
	#   - NZBNA_ in queue-script mode;
	#   - NZBPP_ in pp-script mode.
	Prefix = 'NZBNA_' if 'NZBNA_EVENT' in os.environ else 'NZBPP_'
	
	# Read context (what nzb is currently being processed)
	Category = os.environ[Prefix + 'CATEGORY']
	Directory = os.environ[Prefix + 'DIRECTORY']
	NzbName = os.environ[Prefix + 'NZBNAME']
	
	# Directory for storing list of tested files
	tmp_file_name = os.environ.get('NZBOP_TEMPDIR') + '/PasswordDetector/' + os.environ.get(Prefix + 'NZBID')
	
	# When nzb is added to queue - reorder inner files for earlier fake detection.
	# Also it is possible that nzb was added with a category which doesn't have 
	# FakeDetector listed in the PostScript. In this case FakeDetector was not called
	# when adding nzb to queue but it is being called now and we can reorder
	# files now.
	if os.environ.get('NZBNA_EVENT') == 'NZB_ADDED' or \
		(os.environ.get('NZBNA_EVENT') == 'FILE_DOWNLOADED' and
			os.environ.get('NZBPR_FAKEDETECTOR_SORTED') != 'yes'):
		# Check if previously sorted by FakeDetctor
		if not os.environ.get('NZBPR_FAKEDETECTOR_SORTED') == 'yes':
			print('[INFO] Sorting inner files for earlier fake detection for %s' % NzbName)
			sys.stdout.flush()
			sort_inner_files()
			print('[NZB] NZBPR_FAKEDETECTOR_SORTED=yes')
		if os.environ.get('NZBNA_EVENT') == 'NZB_ADDED':
			sys.exit(POSTPROCESS_NONE)

	print('[DETAIL] Detecting password for %s' % NzbName)
	sys.stdout.flush()
	
	if contains_password(Directory) is True:
		print("[WARNING] Password found in %s" % NzbName)
		# A password is detected
		#
		# Add post-processing parameter "PASSWORDDETECTOR_HASPASSWORD" for nzb-file.
		# Scripts running after password detector can check the parameter like this:
		# if os.environ.get('NZBPR_PASSWORDDETECTOR_HASPASSWORD') == 'yes':
		#     print('Marked as password protected by another script')
		print('[NZB] NZBPR_PASSWORDDETECTOR_HASPASSWORD=yes')

		if os.environ['NZBPO_PASSACTION'] == "Pause":
			pause_nzb(os.environ.get(Prefix + 'NZBID'))
			print("[DETAIL] Paused %s" % NzbName)
			
		if os.environ['NZBPO_PASSACTION'] == "Mark Bad":
		
			# Special command telling NZBGet to mark nzb as bad. The nzb will
			# be removed from queue and become status "FAILURE/BAD".
			print('[NZB] MARK=BAD')
			print("[DETAIL] Marked bad %s" % NzbName)
	else:
		# Not password protected or at least doesn't look like it (yet).
		#
		# When nzb is downloaded again (using "Download again" from history)
		# it may have been marked by our script as protected. Since now the script
		# doesn't consider nzb as protected we remove the old marking. That's
		# of course a rare case that someone will redownload but
		# at least during debugging of password detector we do that all the time.
		if os.environ.get('NZBPR_PASSWORDDETECTOR_HASPASSWORD') == 'yes':
			print('[NZB] NZBPR_PASSWORDDETECTOR_HASPASSWORD=')
		
	print('[DETAIL] Detecting completed for %s' % NzbName)
	sys.stdout.flush()
	
	# Remove temp files in PP
	if Prefix == 'NZBPP_':
		clean_up()


# Execute main script function
main()	

# All OK, returning exit status 'POSTPROCESS_SUCCESS' (int <93>) to let NZBGet know
# that our script has successfully completed (only for pp-script mode).
sys.exit(POSTPROCESS_SUCCESS)
