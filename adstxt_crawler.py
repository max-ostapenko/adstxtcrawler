#!/usr/bin/env python

########################################################################################################
# Copyright 2017 Hebbian Labs, LLC
# Copyright 2017 IAB TechLab & OpenRTB Group
#
# Maintainer: Neal Richter, neal@spotx.tv or nrichter@gmail.com
#
# Redistribution and use in source and binary forms, with or without modification, are permitted provided
# that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice, this list of conditions and the
#    following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice, this list of conditions and
#    the following disclaimer in the documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED
# WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A
# PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
# NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
########################################################################################################


########################################################################################################
# See README.md file
#
# This is a reference implemenation of an ads.txt crawler that downloads, parses and dumps the data to a SQLiteDB
# The code assumes that you have SQLLite installed and created the DB with the associated SQL file
#
# This code would be suitable for a small scale crawler, however it is missing many production features
# for large scale use, such as parallel HTTP download and parsing of the data files, stateful recovery
# of target servers being down, usage of a real production DB server etc.  Use as a reference for your own
# implementations or harden and enhance this code as needed.
#
########################################################################################################

########################################################################################################
# Hat Tips for code contributions
# Ian Trider
# jhpacker
# brk212
# bradlucas
# nag4
# AntoineJac
# markparolisi
# sean-mcmann
# Breza
# miyaichi
########################################################################################################

import sys
import os
import csv
import socket
import sqlite3
import logging
from argparse import ArgumentParser
from urllib.parse import urlparse  # Changed this import
import requests
import re
import tempfile
import multiprocessing
from multiprocessing import Pool
from requests.packages.urllib3.exceptions import InsecureRequestWarning
from datetime import datetime

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

#################################################################
# FUNCTION process_adstxt_row_to_db.
#  handle one row and push to the DB
#
#################################################################


def process_adstxt_row_to_db(conn, data_row, comment, hostname, adsystem_id):
    insert_stmt = "INSERT OR REPLACE INTO adstxt (SITE_DOMAIN, EXCHANGE_DOMAIN, ADSYSTEM_DOMAIN, SELLER_ACCOUNT_ID, ACCOUNT_TYPE, TAG_ID, ENTRY_COMMENT) VALUES (?, ?, ?, ?, ?, ?, ? );"
    exchange_host = ""
    seller_account_id = ""
    account_type = ""
    tag_id = ""
    sql_rows = 0

    if len(data_row) >= 3:
        exchange_host = data_row[0].lower().strip()
        seller_account_id = data_row[1].lower().strip()
        account_type = data_row[2].lower().strip()

    if len(data_row) == 4:
        tag_id = data_row[3].lower().strip()

    # data validation heuristics
    data_valid = 1

    # Minimum length of a domain name is 1 character, not including extensions.
    if len(hostname) < 3:
        data_valid = 0

    if len(exchange_host) < 3:
        data_valid = 0

    if not isinstance(adsystem_id, int):
        data_valid = 0

    # could be single digit integers
    if len(seller_account_id) < 1:
        data_valid = 0

    # ads.txt supports 'DIRECT' and 'RESELLER'
    if account_type not in ["direct", "reseller"]:
        data_valid = 0

    if data_valid > 0:
        logging.debug(
            "%s | %s | %s | %s | %s | %s | %s"
            % (
                hostname,
                exchange_host,
                adsystem_id,
                seller_account_id,
                account_type,
                tag_id,
                comment,
            )
        )

        # Insert a row of data using bind variables (protect against SQL injection)
        c = conn.cursor()
        try:
            c.execute(
                insert_stmt,
                (
                    hostname,
                    exchange_host,
                    adsystem_id,
                    seller_account_id,
                    account_type,
                    tag_id,
                    comment,
                ),
            )
            # Save (commit) the changes
            conn.commit()
            if c.rowcount > 0:
                sql_rows = c.rowcount
        except sqlite3.OperationalError as err:  # Changed exception handling
            print(err)
            print(insert_stmt)
            print(
                hostname,
                exchange_host,
                adsystem_id,
                seller_account_id,
                account_type,
                tag_id,
                comment,
            )

    return sql_rows


# end process_adstxt_row_to_db  #####

#################################################################
# FUNCTION process_contentdirective_row_to_db.
#  handle one row and push to the DB
#
#################################################################


def process_contentdirective_row_to_db(
    conn, case, site_hostname, rhs_hostname, comment
):
    insert_stmt = ""
    sql_rows = 0

    site_hostname = site_hostname.lower().strip()
    rhs_hostname = rhs_hostname.lower().strip()

    data_valid = 1

    # Minimum length of a domain name is 1 character, not including extensions.
    # Domain Name Rules - Nic AG
    # www.nic.ag/rules.htm
    if len(site_hostname) < 3:
        data_valid = 0

    if len(rhs_hostname) < 3:
        data_valid = 0

    if case == "cd":
        insert_stmt = "INSERT OR REPLACE INTO adstxt_contentdistributor (SITE_DOMAIN, PRODUCER_DOMAIN, ENTRY_COMMENT) VALUES (?, ?, ?);"
    elif case == "cp":
        insert_stmt = "INSERT OR REPLACE INTO adstxt_contentproducer (SITE_DOMAIN, DISTRIBUTOR_DOMAIN, ENTRY_COMMENT) VALUES (?, ?, ?);"
    else:
        data_valid = 0

    if data_valid > 0:
        logging.debug(
            "%s | %s | %s | %s" % (case, site_hostname, rhs_hostname, comment)
        )

        # Insert a row of data using bind variables (protect against SQL injection)
        c = conn.cursor()
        try:
            c.execute(insert_stmt, (site_hostname, rhs_hostname, comment))
            # Save (commit) the changes
            conn.commit()
            if c.rowcount > 0:
                sql_rows = c.rowcount
        except sqlite3.OperationalError as err:
            print(err)
            print(insert_stmt)
            print(case, site_hostname, rhs_hostname, comment)

    return sql_rows


# end process_contentdirective_row_to_db  #####


#################################################################
# FUNCTION crawl_to_db.
#  crawl the URLs, parse the data, validate and dump to a DB
#
#################################################################


def crawl_to_db(ahost, referral_domain=False):
    rowcnt = 0
    referral_domains = []

    logging.debug(f"crawl_to_db ({ahost})")

    myheaders = {
        "User-Agent": "AdsTxtCrawler/1.0; +https://github.com/InteractiveAdvertisingBureau/adstxtcrawler",
        "Accept": "text/plain",
    }

    aurl = f"http://{ahost}/ads.txt"
    logging.info(f" Crawling  {aurl} : {ahost} ")

    accept = False
    try:
        r = requests.get(aurl, headers=myheaders, timeout=5)
        logging.info(f"  {r.status_code}")
        if r.status_code == 200:
            text = r.text.lower()
            # ignore html file
            if (
                "html" not in text
                and "body" not in text
                and "div" not in text
                and "span" not in text
            ):
                accept = True
    except:
        pass

    if accept:
        text = re.sub(re.compile("[^\x20-\x7E\r\n]"), "", r.text)

        logging.debug("-------------")
        logging.debug(r.request.headers)
        logging.debug("-------------")
        logging.debug(f"{text}")
        logging.debug("-------------")

        with tempfile.NamedTemporaryFile(delete=False) as tmp_csv_file:
            tmpfile = tmp_csv_file.name
            tmp_csv_file.write(text.encode("utf-8"))

        with open(tmpfile, "r", encoding="utf-8", newline="") as tmp_csv_file:
            # read the line, split on first comment and keep what is to the
            # left (if any found)
            line_reader = csv.reader(tmp_csv_file, delimiter="#", quotechar="|")
            comment = ""

            conn = sqlite3.connect(database, timeout=10)
            with conn:
                for line in line_reader:
                    logging.debug(f"DATA:  {line}")

                    try:
                        data_line = line[0]
                    except:
                        data_line = ""

                    # determine delimiter, conservative = do it per row
                    data_delimiter = ","
                    if data_line.find("\t") != -1:
                        data_delimiter = "\t"

                    data_reader = csv.reader(
                        [data_line], delimiter=data_delimiter, quotechar="|"
                    )
                    for row in data_reader:
                        if len(row) > 0 and row[0].startswith("#"):
                            continue

                        directive_pattern = "domain="
                        if len(row) > 0 and directive_pattern in row[0].lower():
                            logging.debug(f"DIRECTIVE [{row}]")
                            s = row[0].lower().split("=")
                            rhs_host = ""
                            comment = ""
                            if len(s) > 1:
                                lhs = s[0].strip()
                                rhs = s[1].strip().split("#")

                                if len(rhs) > 0:
                                    rhs_host = rhs[0].strip().lower()
                                if len(rhs) > 1:
                                    comment = rhs[1].strip()

                                if lhs.startswith("subdomain"):
                                    logging.debug(f"DIRECTIVE subdomain:[{rhs_host}]")
                                    referral_domains.append(rhs_host)

                                elif lhs.startswith("contentproducerdomain"):
                                    logging.debug(
                                        f"DIRECTIVE contentproducerdomain [{ahost}][{rhs_host}]"
                                    )
                                    referral_domains.append(rhs_host)
                                    rowcnt += process_contentdirective_row_to_db(
                                        conn, "cd", ahost, rhs_host, comment
                                    )

                                elif lhs.startswith("contentdistributordomain"):
                                    logging.debug(
                                        f"contentdistributordomain [{ahost}][{rhs_host}]"
                                    )
                                    referral_domains.append(rhs_host)
                                    rowcnt += process_contentdirective_row_to_db(
                                        conn, "cp", ahost, rhs_host, comment
                                    )

                        # skip row if it's not at least 3 fields
                        if len(row) < 3:
                            continue

                        if len(line) > 1 and len(line[1]) > 0:
                            comment = line[1]

                        adsystem_domain = row[0].lower().strip()
                        adsystem_id = fetch_adsystem_id(conn, adsystem_domain)

                        if not (adsystem_id is not None and adsystem_id > 0):
                            logging.warning(
                                f"FIX unknown ADSYSTEM [{adsystem_domain}][{row[0]}]"
                            )

                        rowcnt += process_adstxt_row_to_db(
                            conn, row, comment, ahost, adsystem_id
                        )

        os.remove(tmpfile)

    if not referral_domain:
        for ahost in referral_domains:
            rowcnt += crawl_to_db(ahost, referral_domain=True)

    return rowcnt


# end crawl_to_db  #####

#################################################################
# FUNCTION load_url_queue
#  Load the target set of URLs and reduce to an ads.txt domains queue
#
#################################################################


def load_url_queue(csvfilename, url_queue):
    cnt = 0

    with open(
        csvfilename, "r", newline="", encoding="utf-8"
    ) as csvfile:  # Adjusted the file mode
        targets_reader = csv.reader(csvfile, delimiter=",", quotechar="|")
        for row in targets_reader:
            if len(row) < 1 or row[0].startswith("#"):
                continue

            for item in row:
                host = "localhost"

                if "http:" in item or "https:" in item:
                    logging.info("URL: %s" % item)
                    parsed_uri = urlparse(row[0])
                    host = parsed_uri.netloc
                else:
                    host = item
                    logging.info("HOST: %s" % item)

            skip = 0

            try:
                # Uncomment next line to print host
                # print("Checking DNS: %s" % str(host))
                ip = socket.gethostbyname(host)

                if "127.0.0" in ip:
                    skip = 0  # swap to 1 to skip localhost testing
                elif "0.0.0.0" in ip:
                    skip = 1
                else:
                    logging.info("  Validated Host IP: %s" % ip)
            except:
                skip = 1

            if skip < 1:
                ads_txt_url = "http://{thehost}/ads.txt".format(
                    thehost=host
                )  # Removed the encode to utf-8
                logging.info("  pushing %s" % ads_txt_url)
                # url_queue[ads_txt_url] = host
                url_queue.append(host)  # Adjusted to append the host to the queue
                cnt += 1

    return cnt


# end load_url_queue  #####

#################################################################
# FUNCTION fetch_adsystem_id
#  fetch adsystem ID from 'adsystem_domain' table
#
#################################################################


def fetch_adsystem_id(conn, adsystem_domain):
    select_stmt = "SELECT ID FROM adsystem_domain WHERE DOMAIN=?"
    c = conn.cursor()
    c.execute(select_stmt, (adsystem_domain,))

    ads_id = None
    try:
        ads_id = c.fetchone()[0]
    except TypeError:  # Most likely exception for this context
        ads_id = None

    return ads_id


# end fetch_adsystem_id  #####

#################################################################
# FUNCTION update_adsystem_domain
#  update ADSYSTEM_DOMAIN
#
#################################################################


def update_adsystem_domain(
    database,
):  # Added 'database' parameter for clarity and reusability
    update_stmt = """UPDATE ADSTXT 
                     SET ADSYSTEM_DOMAIN = (SELECT IFNULL(ID,0) 
                                            FROM ADSYSTEM_DOMAIN 
                                            WHERE ADSTXT.EXCHANGE_DOMAIN = ADSYSTEM_DOMAIN.DOMAIN) 
                     WHERE EXISTS (SELECT * 
                                   FROM ADSYSTEM_DOMAIN 
                                   WHERE ADSTXT.EXCHANGE_DOMAIN = ADSYSTEM_DOMAIN.DOMAIN);"""
    conn = sqlite3.connect(database, timeout=10)
    with conn:
        c = conn.cursor()
        c.execute(update_stmt)


# end update_adsystem_domain  #####

#################################################################
# FUNCTION set_log_file
# setup the log file
#
#################################################################


def set_log_file(log_level_num):
    """
    Create a log file for the job
    """
    file_name = "adstxt_crawler.log"
    log_format = "%(asctime)s %(filename)s:%(lineno)d:%(levelname)s  %(message)s"

    if log_level_num == 1:
        log_level = logging.INFO
    elif log_level_num == 2:
        log_level = logging.DEBUG
    else:
        log_level = logging.WARNING

    logging.basicConfig(filename=file_name, level=log_level, format=log_format)


# end set_log_file  #####

#################################################################
# FUNCTION init_database
# initialize connection and test the DB is alive
#
#################################################################


def init_database(db_name):
    """
    Setup the DB connection and seed with data if needed
    """
    conn = sqlite3.connect(db_name)

    select_stmt = "SELECT count(*) FROM adsystem_domain"
    c = conn.cursor()
    c.execute(select_stmt)

    cnt = None
    try:
        cnt = c.fetchone()[0]
    except TypeError:  # Likely exception when fetchone() returns None
        cnt = 0

    # Closing connection is a good practice after you're done with it
    conn.close()

    return cnt


# end init_database  #####

#### MAIN ####
# if __name__ == "__main__":
# Set default values for persistent data
crawl_url_queue = []
cnt_urls = 0
cnt_records = 0
database = ""
cnt_urls_processed = 0  # TBA

arg_parser = ArgumentParser()
arg_parser.add_argument(
    "-t",
    "--targets",
    dest="target_filename",
    help="list of domains to crawl ads.txt from",
    metavar="FILE",
)
arg_parser.add_argument(
    "-d",
    "--database",
    dest="target_database",
    help="Database to dump crawled data into",
    metavar="FILE",
)
arg_parser.add_argument(
    "-v",
    "--verbose",
    dest="verbose",
    action="count",
    help="Increase verbosity (specify multiple times for more)",
)
arg_parser.add_argument(
    "-p",
    "--thread_pool",
    dest="num_threads",
    default=4,
    type=int,
    help="number of crawling threads to use",
)

args = arg_parser.parse_args()

# Exit with help info if no args passed
if len(sys.argv) == 1:
    arg_parser.print_help()
    exit(1)

print(args)

set_log_file(args.verbose)
red_log_template = f"\033[91m{{}} \033[0m"
green_log_template = f"\033[92m{{}} \033[0m"

# Exit with help if no DB file passed
if args.target_database and len(args.target_database) > 1:
    ret = init_database(args.target_database)
    if ret < 1:
        print(red_log_template.format("Missing Database"))
        arg_parser.print_help()
        exit(1)
    database = args.target_database
else:
    print(red_log_template.format("Missing Database file name argument"))
    arg_parser.print_help()
    exit(1)

# Exit with help if no target domains file passed
if args.target_filename and len(args.target_filename) > 1:
    cnt_urls = load_url_queue(args.target_filename, crawl_url_queue)
else:
    print(red_log_template.format("Missing target domains file name argument"))
    arg_parser.print_help()
    exit(1)

if cnt_urls < 1 or not database or len(database) < 1:
    print("No Crawl")
    logging.warning("No Crawl")
    exit(1)

if args.num_threads > 1:
    print(f"Thread Pool Crawl {args.num_threads}")
    logging.warning(f"Thread Pool Crawl {args.num_threads}")
    p = Pool(args.num_threads)  # OR multiprocessing.cpu_count()
    records = p.map(crawl_to_db, crawl_url_queue)
    p.close()
    p.join()
    cnt_records = sum(records)
else:
    print("Single Threaded Crawl")
    logging.warning("Single Threaded Crawl")
    cnt_records = 0
    for row in crawl_url_queue:
        cnt_records += crawl_to_db(row)

print(
    green_log_template.format(
        f"Wrote {cnt_records} records from {cnt_urls} URLs to {args.target_database}"
    )
)
logging.warning(
    f"Wrote {cnt_records} records from {cnt_urls} URLs to {args.target_database}"
)
logging.warning("Finished crawl.")
