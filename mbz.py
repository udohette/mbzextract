import os
import zipfile
import tarfile
import sys
import tempfile
import shutil
import re
import xml.etree.ElementTree as et
import sqlite3
import magic
import importlib
from datetime import datetime
from html.parser import HTMLParser

class MBZ:
    def __init__(self,output):
        if output == None:
            self.out_dir = "."
        else:
            self.out_dir = output

        # create temporary directory for sqlite database and file extraction
        self.temp_dir = tempfile.mkdtemp()

        # create the database and cursor
        self.db_file = self.temp_dir + "/moodle.db"
        self.db = sqlite3.connect(self.db_file, detect_types=sqlite3.PARSE_DECLTYPES)
        self.db_cursor = self.db.cursor()

        # create a table for course information
        query = "CREATE TABLE course (fullname text, shortname text, moodle_release text, startdate int, www_root text)"
        self.db_cursor.execute(query)

        # create a table for list of activites and resources
        query = "CREATE TABLE activities (moduleid int, modulename text, title text, directory text, sectionid int)"
        self.db_cursor.execute(query)

        # create a table for sections
        query = "CREATE TABLE sections (sectionid int,title text,directory text)"
        self.db_cursor.execute(query)

        # create a table for users
        query = "CREATE TABLE users (userid int, firstname text, lastname text, email text)"
        self.db_cursor.execute(query)

        # create a table for files
        query = "CREATE TABLE files (id int, contenthash text, contextid int, filename text, mime text)"
        self.db_cursor.execute(query)

        # commit the transaction
        self.db.commit()

        # TODO: There probably should be a table created for grades, but that is for a future revision

    def parse_backup(self,backup_file):
        """Open the moodle_backup.xml and files.xml files and parse the contents into the database"""
        print("Parsing the moodle backup file...")
        self.backup = mbzFile(backup_file)

        # try opening the moodle_backup.xml file and create the moodle_backup object
        try:
            self.moodle_backup = et.parse(self.backup.open('moodle_backup.xml')).getroot()
            self.moodle_files = et.parse(self.backup.open('files.xml')).getroot()

            # check to see if this backup file has users
            if self.moodle_backup.find('./information/settings/setting/[name="users"]/value').text == "1":
                self.moodle_users = et.parse(self.backup.open('users.xml')).getroot()
                # add users into the database we just created
                for user in self.moodle_users.findall('./user'):
                    user_info = (user.get('id'),
                        user.find('firstname').text,
                        user.find('lastname').text,
                        user.find('email').text)
                    self.db_cursor.execute('INSERT INTO users VALUES(?,?,?,?)',user_info)
                self.user_data = True

            # grab course information
            course_info = (self.moodle_backup.find('./information/original_course_fullname').text,
                self.moodle_backup.find('./information/original_course_shortname').text,
                self.moodle_backup.find('./information/moodle_release').text,
                self.moodle_backup.find('./information/original_course_startdate').text,
                self.moodle_backup.find('./information/original_wwwroot').text)
            self.db_cursor.execute('INSERT INTO course VALUES (?,?,?,?,?)',course_info)
            self.course = self.moodle_backup.find('./information/original_course_fullname').text

        except KeyError:
            sys.exit('The backup file provided does not seem to be a standard Moodle backup file. Exiting.')

        # sections first
        for section in self.moodle_backup.findall('./information/contents/sections/section'):
            section_info = (section.find('sectionid').text,
                section.find('title').text,
                section.find('directory').text)
            self.db_cursor.execute('INSERT INTO sections VALUES(?,?,?)',section_info)
        # activities next

        for activity in self.moodle_backup.findall('./information/contents/activities/activity'):
            activity_info = (activity.find('moduleid').text,
                activity.find('modulename').text,
                activity.find('title').text,
                activity.find('directory').text,
                activity.find('sectionid').text)
            self.db_cursor.execute('INSERT INTO activities VALUES(?,?,?,?,?)',activity_info)

        # then files

        for f in self.moodle_files.findall('./file'):
            file_info = (f.get('id'),
                f.find('contenthash').text,
                f.find('contextid').text,
                f.find('filename').text,
                f.find('mimetype').text)
            self.db_cursor.execute('INSERT INTO files VALUES (?,?,?,?,?)',file_info)

        self.db.commit()

    def extract(self):

        # create the output directory for extracting the contents
        if os.path.exists(self.out_dir+"/"+self.course) == False:
            os.mkdir(self.out_dir+"/"+self.course)
        os.chdir(self.out_dir+"/"+self.course)
        self.final_dir = os.getcwd()
        print(self.final_dir)

        # create directory structure by section
        self.db_cursor.execute('SELECT * FROM sections')
        for section in self.db_cursor.fetchall():
            if os.path.exists("Section - "+self.stripped(section[1])+"_"+str(section[0])) == False:
                os.mkdir("Section - "+self.stripped(section[1])+"_"+str(section[0]))
            os.chdir("Section - "+self.stripped(section[1])+"_"+str(section[0]))

            # create a directory for files if this section has files associated with it
            # that are not part of an activity
            inforef_xml = et.parse(self.backup.open(section[2]+"/inforef.xml")).getroot()
            if inforef_xml.find('./fileref') is not None:
                if os.path.exists('files') == False:
                    os.mkdir('files')

                # extract the files
                os.chdir(self.temp_dir)
                for f in inforef_xml.findall('./fileref/file'):
                    self.db_cursor.execute('SELECT contenthash,filename FROM files WHERE filename != "." and id=?',(f.find('id').text,))
                    results = self.db_cursor.fetchone()
                    if results is not None:
                        self.backup.extract('files/'+results[0][:2]+'/'+results[0])
                        shutil.move(self.temp_dir+"/files/"+results[0][:2]+"/"+results[0],self.final_dir+"/Section - "+self.stripped(section[1])+"_"+str(section[0])+"/files/"+results[1])
                os.chdir(self.final_dir+"/Section - "+self.stripped(section[1])+"_"+str(section[0]))


            # fetch the activities in this section
            self.db_cursor.execute('SELECT modulename,moduleid,directory FROM activities WHERE sectionid=?',(section[0],))
            activities = self.db_cursor.fetchall()
            for activity in activities:
                try:
                    plugin = importlib.import_module("plugins."+activity[0]+"."+activity[0])
                    print('\033[32;22mProcessing\033[0m', activity[0],activity[1])
                except ImportError:
                    print('\033[31;22mSkipping\033[0m',activity[0])
                    continue
                mod = plugin.moodle_module(self.backup,self.temp_dir,self.db,activity[2],self.final_dir+"/Section - "+self.stripped(section[1])+"_"+str(section[0]),self.user_data)
                mod.parse()
                mod.extract()
            os.chdir("..")

        # create a copy of the sqlite database in the extracted folder
        shutil.copy(self.temp_dir+"/moodle.db",self.final_dir+"/"+"backup_database.db")

        # create a readme file in the folder
        f = open(self.final_dir+"/readme.txt",'w')
        f.write("The folders are organized by the sections as they existed in your moodle course. Inside each section folder is a folder for each activity or resource that existed. In each activity or resource folder is a folder called 'files' that contains the files from that activity or resource.")
        f.close()
    def clean(self):
        shutil.rmtree(self.temp_dir)

    def stripped(self,x):
        the_string = "".join([i for i in x if 31 < ord(i) < 127])
        the_string = the_string.strip()
        the_string = re.sub(r'[^\w]','_',the_string)
        return the_string

class mbzFile(MBZ):

    """This class is intended to deal with the fact that the moodle backup files
     can come in two flavors, zip and gzip. The python libraries for both
     vary slightly."""

    def __init__(self,backup_file):

        if magic.from_file(backup_file,mime=True) == b'application/zip':
            self.backup_type = "zip"
        elif magic.from_file(backup_file,mime=True) == b'application/x-gzip':
            self.backup_type = "gzip"
        self.file = backup_file

    def open(self,f):

        if self.backup_type == "zip":
            backup = zipfile.ZipFile(self.file,'r')
            return backup.open(f)

        elif self.backup_type == "gzip":
            backup = tarfile.open(self.file,'r')
            return backup.extractfile(f)

    def extract(self,f):

        if self.backup_type == "zip":
            backup = zipfile.ZipFile(self.file,'r')
            return backup.extract(f)

        elif self.backup_type == "gzip":
            backup = tarfile.open(self.file,'r')
            return backup.extract(f)
