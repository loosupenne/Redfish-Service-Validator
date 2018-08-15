
# Copyright Notice:
# Copyright 2016 Distributed Management Task Force, Inc. All rights reserved.
# License: BSD 3-Clause License. For full text see link: https://github.com/DMTF/Redfish-Service-Validator/blob/master/LICENSE.md

import requests
import sys
import re
import os
import json
import random
from collections import OrderedDict
from functools import lru_cache
import logging
from rfSession import rfSession
from requests.packages.urllib3.exceptions import InsecureRequestWarning
from http.client import responses
import copy
import configparser

import metadata as md
from commonRedfish import *
import rfSchema


traverseLogger = logging.getLogger(__name__)
traverseLogger.setLevel(logging.DEBUG)
ch = logging.StreamHandler(sys.stdout)
ch.setLevel(logging.INFO)
traverseLogger.addHandler(ch)

commonHeader = {'OData-Version': '4.0'}
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

# dictionary to hold sampling notation strings for URIs
uri_sample_map = dict()

currentService = None


class AuthenticationError(Exception):
    """Exception used for failed basic auth or token auth"""
    def __init__(self, msg=None):
        super(AuthenticationError, self).__init__(msg)


def getLogger():
    """
    Grab logger for tools that might use this lib
    """
    return traverseLogger

# default config
argparse2configparser = {
        'user': 'username', 'nochkcert': '!certificatecheck', 'ca_bundle': 'certificatebundle', 'schemamode': 'schemamode',
        'suffix': 'schemasuffix', 'schemadir': 'metadatafilepath', 'nossl': '!usessl', 'timeout': 'timeout', 'service': 'servicemode',
        'http_proxy': 'httpproxy', 'localonly': 'localonlymode', 'https_proxy': 'httpsproxy', 'passwd': 'password',
        'ip': 'targetip', 'logdir': 'logpath', 'desc': 'systeminfo', 'authtype': 'authtype',
        'payload': 'payloadmode+payloadfilepath', 'cache': 'cachemode+cachefilepath', 'token': 'token',
        'linklimit': 'linklimit', 'sample': 'sample', 'nooemcheck': '!oemcheck'
        }

configset = {
        "targetip": str, "username": str, "password": str, "authtype": str, "usessl": bool, "certificatecheck": bool, "certificatebundle": str,
        "metadatafilepath": str, "cachemode": (bool, str), "cachefilepath": str, "schemasuffix": str, "timeout": int, "httpproxy": str, "httpsproxy": str,
        "systeminfo": str, "localonlymode": bool, "servicemode": bool, "token": str, 'linklimit': dict, 'sample': int, 'extrajsonheaders': dict, 'extraxmlheaders': dict, "schema_pack": str,
        "forceauth": bool, "oemcheck": bool
        }

defaultconfig = {
        'authtype': 'basic', 'username': "", 'password': "", 'token': '', 'oemcheck': True,
        'certificatecheck': True, 'certificatebundle': "", 'metadatafilepath': './SchemaFiles/metadata',
        'cachemode': 'Off', 'cachefilepath': './cache', 'schemasuffix': '_v1.xml', 'httpproxy': "", 'httpsproxy': "",
        'localonlymode': False, 'servicemode': False, 'linklimit': {'LogEntry': 20}, 'sample': 0, 'schema_pack': None, 'forceauth': False
        }

config = dict(defaultconfig)

configSet = False


def startService():
    global currentService
    if currentService is not None:
        currentService.close()
    currentService = rfService(config)
    return currentService

def convertConfigParserToDict(configpsr):
    cdict = {}
    for category in configpsr:
        for option in configpsr[category]:
            val = configpsr[category][option]
            if option not in configset.keys() and category not in ['Information', 'Validator']:
                traverseLogger.error('Config option {} in {} unsupported!'.format(option, category))
            if val in ['', None]:
                continue
            if val.isdigit():
                val = int(val)
            elif option == 'linklimit':
                val = re.findall('[A-Za-z_]+:[0-9]+', val)
            elif str(val).lower() in ['on', 'true', 'yes']:
                val = True
            elif str(val).lower() in ['off', 'false', 'no']:
                val = False
            cdict[option] = val
    return cdict


def setByArgparse(args):
    ch.setLevel(args.verbose_checks)
    if args.v:
        ch.setLevel(logging.DEBUG)
    if args.config is not None:
        configpsr = configparser.ConfigParser()
        configpsr.read(args.config)
        cdict = convertConfigParserToDict(configpsr)
    else:
        cdict = {}
        for param in args.__dict__:
            if param in argparse2configparser:
                if isinstance(args.__dict__[param], list):
                    for cnt, item in enumerate(argparse2configparser[param].split('+')):
                        cdict[item] = args.__dict__[param][cnt]
                elif '+' not in argparse2configparser[param]:
                    if '!' in argparse2configparser[param]:
                        cdict[argparse2configparser[param].replace('!', '')] = not args.__dict__[param]
                    else:
                        cdict[argparse2configparser[param]] = args.__dict__[param]
            else:
                cdict[param] = args.__dict__[param]

    setConfig(cdict)


def setConfig(cdict):
    """
    Set config based on configurable dictionary
    """
    # Send config only with keys supported by program
    linklimitdict = {}
    if cdict.get('linklimit') is not None:
        for item in cdict.get('linklimit'):
            if re.match('[A-Za-z_]+:[0-9]+', item) is not None:
                typename, count = tuple(item.split(':')[:2])
                if typename not in linklimitdict:
                    linklimitdict[typename] = int(count)
                else:
                    traverseLogger.error('Limit already exists for {}'.format(typename))
    cdict['linklimit'] = linklimitdict

    for item in cdict:
        if item not in configset:
            traverseLogger.debug('Unsupported {}'.format(item))
        elif not isinstance(cdict[item], configset[item]):
            traverseLogger.error('Unsupported {}, expected type {}'.format(item, configset[item]))

    global config
    config = dict(defaultconfig)

    # set linklimit
    defaultlinklimit = config['linklimit']

    config.update(cdict)

    config['configuri'] = ('https' if config.get('usessl', True) else 'http') + '://' + config['targetip']
    config['certificatecheck'] = config.get('certificatecheck', True) and config.get('usessl', True)

    defaultlinklimit.update(config['linklimit'])
    config['linklimit'] = defaultlinklimit

    if config['cachemode'] not in ['Off', 'Fallback', 'Prefer']:
        if config['cachemode'] is not False:
            traverseLogger.error('CacheMode or path invalid, defaulting to Off')
        config['cachemode'] = 'Off'

    AuthType = config['authtype']
    if AuthType not in ['None', 'Basic', 'Session', 'Token']:
        config['authtype'] = 'Basic'
        traverseLogger.error('AuthType invalid, defaulting to Basic')


class rfService():
    def __init__(self, config):
        traverseLogger.info('Setting up service...')
        global currentService
        currentService = self
        self.config = config
        self.proxies = dict()
        self.active = False

        config['configuri'] = ('https' if config.get('usessl', True) else 'http') + '://' + config['targetip']
        httpprox = config['httpproxy']
        httpsprox = config['httpsproxy']
        self.proxies['http'] = httpprox if httpprox != "" else None
        self.proxies['https'] = httpsprox if httpsprox != "" else None

        # Convert list of strings to dict
        self.chkcertbundle = config['certificatebundle']
        chkcertbundle = self.chkcertbundle
        if chkcertbundle not in [None, ""] and config['certificatecheck']:
            if not os.path.isfile(chkcertbundle) and not os.path.isdir(chkcertbundle):
                self.chkcertbundle = None
                traverseLogger.error('ChkCertBundle is not found, defaulting to None')
        else:
            config['certificatebundle'] = None

        ChkCert = config['certificatecheck']
        AuthType = config['authtype']

        self.currentSession = None
        if not config.get('usessl', True) and not config['forceauth']:
            if config['username'] not in ['', None] or config['password'] not in ['', None]:
                traverseLogger.warning('Attempting to authenticate on unchecked http/https protocol is insecure, if necessary please use ForceAuth option.  Clearing auth credentials...')
                config['username'] = ''
                config['password'] = ''
        if AuthType == 'Session':
            certVal = chkcertbundle if ChkCert and chkcertbundle is not None else ChkCert
            # no proxy for system under test
            self.currentSession = rfSession(config['username'], config['password'], config['configuri'], None, certVal, self.proxies)
            self.currentSession.startSession()
        self.metadata = md.Metadata(traverseLogger)
        self.active = True

    def close(self):
        if self.currentSession is not None and self.currentSession.started:
            self.currentSession.killSession()
        self.active = False

def navigateJsonFragment(decoded, URILink):
    if '#' in URILink:
        URILink, frag = tuple(URILink.rsplit('#', 1))
        fragNavigate = frag.split('/')
        for item in fragNavigate:
            if item == '':
                continue
            if isinstance(decoded, dict):
                decoded = decoded.get(item)
            elif isinstance(decoded, list):
                if not item.isdigit():
                    traverseLogger.error("This is an Array, but this is not an index, aborting: {} {}".format(URILink, item))
                    return None
                decoded = decoded[int(item)] if int(item) < len(decoded) else None
        if not isinstance(decoded, dict):
            traverseLogger.error(
                "Decoded object no longer a dictionary {}".format(URILink))
            return None
    return decoded


@lru_cache(maxsize=64)
def callResourceURI(URILink):
    """
    Makes a call to a given URI or URL

    param arg1: path to URI "/example/1", or URL "http://example.com"
    return: (success boolean, data, request status code)
    """
    # rs-assertions: 6.4.1, including accept, content-type and odata-versions
    # rs-assertion: handle redirects?  and target permissions
    # rs-assertion: require no auth for serviceroot calls
    if URILink is None:
        traverseLogger.warn("This URI is empty!")
        return False, None, -1, 0

    if currentService is None:
        traverseLogger.warn("The current service is not setup!  Program must configure the service before contacting URIs")

    URILink = URILink.rstrip('/')
    config = currentService.config
    proxies = currentService.proxies
    ConfigURI, UseSSL, AuthType, ChkCert, ChkCertBundle, timeout, Token = config['configuri'], config['usessl'], config['authtype'], \
            config['certificatecheck'], config['certificatebundle'], config['timeout'], config['token']
    CacheMode, CacheDir = config['cachemode'], config['cachefilepath']

    nonService = isNonService(URILink)
    payload, statusCode, elapsed, auth, noauthchk = None, '', 0, None, True

    isXML = False
    if "$metadata" in URILink or ".xml" in URILink:
        isXML = True
        traverseLogger.debug('Should be XML')

    ExtraHeaders = None
    if 'extrajsonheaders' in config and not isXML:
        ExtraHeaders = eval(config['extrajsonheaders'])
    elif 'extraxmlheaders' in config and isXML:
        ExtraHeaders = eval(config['extraxmlheaders'])

    # determine if we need to Auth...
    if not nonService:
        noauthchk =  URILink in ['/redfish', '/redfish/v1', '/redfish/v1/odata'] or\
            '/redfish/v1/$metadata' in URILink
        if noauthchk:
            traverseLogger.debug('dont chkauth')
            auth = None
        else:
            auth = (config['username'], config['password'])
        if CacheMode in ["Fallback", "Prefer"]:
            CacheDir = os.path.join(CacheDir + URILink)
            if os.path.isfile(CacheDir):
                with open(CacheDir) as f:
                    payload = f.read()
            if os.path.isfile(os.path.join(CacheDir, 'index.xml')):
                with open(os.path.join(CacheDir, 'index.xml')) as f:
                    payload = f.read()
            if os.path.isfile(os.path.join(CacheDir, 'index.json')):
                with open(os.path.join(CacheDir, 'index.json')) as f:
                    payload = json.loads(f.read())
                payload = navigateJsonFragment(payload, URILink)
    if nonService and config['servicemode']:
        traverseLogger.warning('Disallowed out of service URI')
        return False, None, -1, 0

    # rs-assertion: do not send auth over http
    # remove UseSSL if necessary if you require unsecure auth
    if (not UseSSL and not config['forceauth']) or nonService or AuthType != 'Basic':
        auth = None

    # only send token when we're required to chkauth, during a Session, and on Service and Secure
    if UseSSL and not nonService and AuthType == 'Session' and not noauthchk:
        currentSession = currentService.currentSession
        headers = {"X-Auth-Token": currentSession.getSessionKey()}
        headers.update(commonHeader)
    elif UseSSL and not nonService and AuthType == 'Token' and not noauthchk:
        headers = {"Authorization": "Bearer "+Token}
        headers.update(commonHeader)
    else:
        headers = copy.copy(commonHeader)

    if ExtraHeaders is not None:
        headers.update(ExtraHeaders)

    certVal = ChkCertBundle if ChkCert and ChkCertBundle not in [None, ""] else ChkCert

    # rs-assertion: must have application/json or application/xml
    traverseLogger.debug('callingResourceURI{}with authtype {} and ssl {}: {} {}'.format(
        ' out of service ' if nonService else ' ', AuthType, UseSSL, URILink, headers))
    try:
        if payload is not None and CacheMode == 'Prefer':
            return True, payload, -1, 0
        response = requests.get(ConfigURI + URILink if not nonService else URILink,
                                headers=headers, auth=auth, verify=certVal, timeout=timeout,
                                proxies=proxies if nonService else None)  # only proxy non-service
        expCode = [200]
        elapsed = response.elapsed.total_seconds()
        statusCode = response.status_code
        traverseLogger.debug('{}, {}, {},\nTIME ELAPSED: {}'.format(statusCode,
                             expCode, response.headers, elapsed))
        if statusCode in expCode:
            contenttype = response.headers.get('content-type')
            if contenttype is not None and 'application/json' in contenttype:
                traverseLogger.debug("This is a JSON response")
                decoded = response.json(object_pairs_hook=OrderedDict)
                # navigate fragment
                decoded = navigateJsonFragment(decoded, URILink)
                if decoded is None:
                    traverseLogger.error(
                            "The JSON pointer in the fragment of this URI is not constructed properly: {}".format(URILink))
            elif contenttype is not None and 'application/xml' in contenttype:
                decoded = response.text
            elif nonService and contenttype is not None and 'text/xml' in contenttype:
                # non-service schemas can use "text/xml" Content-Type
                decoded = response.text
            else:
                traverseLogger.error(
                        "This URI did NOT return XML or Json, this is not a Redfish resource (is this redirected?): {}".format(URILink))
                return False, response.text, statusCode, elapsed
            return decoded is not None, decoded, statusCode, elapsed
        elif statusCode == 401:
            if not nonService and AuthType in ['Basic', 'Token']:
                if AuthType == 'Token':
                    cred_type = 'token'
                else:
                    cred_type = 'username and password'
                raise AuthenticationError('Error accessing URI {}. Status code "{} {}". Check {} supplied for "{}" authentication.'
                                          .format(URILink, statusCode, responses[statusCode], cred_type, AuthType))

    except requests.exceptions.SSLError as e:
        traverseLogger.error("SSLError on {}".format(URILink))
        traverseLogger.debug("output: ", exc_info=True)
    except requests.exceptions.ConnectionError as e:
        traverseLogger.error("ConnectionError on {}".format(URILink))
        traverseLogger.debug("output: ", exc_info=True)
    except requests.exceptions.Timeout as e:
        traverseLogger.error("Request has timed out ({}s) on resource {}".format(timeout, URILink))
        traverseLogger.debug("output: ", exc_info=True)
    except requests.exceptions.RequestException as e:
        traverseLogger.error("Request has encounted a problem when getting resource {}".format(URILink))
        traverseLogger.warning("output: ", exc_info=True)
    except AuthenticationError as e:
        raise e  # re-raise exception
    except Exception:
        traverseLogger.error("A problem when getting resource has occurred {}".format(URILink))
        traverseLogger.warning("output: ", exc_info=True)

    if payload is not None and CacheMode == 'Fallback':
        return True, payload, -1, 0
    return False, None, statusCode, elapsed


def createResourceObject(name, uri, jsondata=None, typename=None, context=None, parent=None, isComplex=False):
    """
    Factory for resource object, move certain work here
    """
    traverseLogger.debug(
        'Creating ResourceObject {} {} {}'.format(name, uri, typename))

    # Create json from service or from given
    if jsondata is None and not isComplex:
        success, jsondata, status, rtime = callResourceURI(uri)
        traverseLogger.debug('{}, {}, {}'.format(success, jsondata, status))
        if not success:
            traverseLogger.error(
                '{}:  URI could not be acquired: {}'.format(uri, status))
            return None
    else:
        jsondata, rtime = jsondata, 0

    if not isinstance(jsondata, dict):
        if not isComplex:
            traverseLogger.error("Resource no longer a dictionary...")
        else:
            traverseLogger.debug("ComplexType does not have val")
        return None

    newResource = ResourceObj(name, uri, jsondata, typename, context, parent, isComplex)
    newResource.rtime = rtime

    return newResource


class ResourceObj:
    robjcache = {}

    def __init__(self, name: str, uri: str, jsondata: dict, typename: str, context: str, parent=None, isComplex=False):
        self.initiated = False
        self.parent = parent
        self.uri, self.name = uri, name
        self.rtime = 0
        self.isRegistry = False
        self.errorindex = {
                "badtype": 0

        }

        # Check if this is a Registry resource
        parent_type = parent.typeobj.stype if parent is not None and parent.typeobj is not None else None
        if parent_type == 'MessageRegistryFile':
            traverseLogger.debug('{} is a Registry resource'.format(self.uri))
            self.isRegistry = True

        # Check if we provide a valid json
        self.jsondata = jsondata

        traverseLogger.debug("payload: {}".format(json.dumps(self.jsondata, indent=4, sort_keys=True)))

        if not isinstance(self.jsondata, dict):
            traverseLogger.error("Resource no longer a dictionary...")
            raise ValueError('This Resource is no longer a Dictionary')

        # Check if this is a Registry resource
        parent_type = parent.typeobj.stype if parent is not None and parent.typeobj is not None else None

        # Check for @odata.id (todo: regex)
        odata_id = self.jsondata.get('@odata.id')
        if odata_id is None and not isComplex:
            if self.isRegistry:
                traverseLogger.debug('{}: @odata.id missing, but not required for Registry resource'
                                     .format(self.uri))
            else:
                traverseLogger.error('{}: Json does not contain @odata.id'.format(self.uri))

        # Get our real type (check for version)
        acquiredtype = jsondata.get('@odata.type', typename)
        if acquiredtype is None:
            traverseLogger.error(
                '{}:  Json does not contain @odata.type or NavType'.format(uri))
            raise ValueError
        if acquiredtype is not typename and isComplex:
            context = None

        if currentService:
            if jsondata.get('@odata.type') is not None:
                currentService.metadata.add_service_namespace(getNamespace(jsondata.get('@odata.type')))
            if jsondata.get('@odata.context') is not None:
                # add the namespace to the set of namespaces referenced by this service
                ns = getNamespace(jsondata.get('@odata.context').split('#')[-1])
                if '/' not in ns and not ns.endswith('$entity'):
                    currentService.metadata.add_service_namespace(ns)

        # Provide a context for this (todo: regex)
        if context is None:
            context = self.jsondata.get('@odata.context')
            if context is None and not isComplex:
                context = createContext(acquiredtype)
                if self.isRegistry:
                    # If this is a Registry resource, @odata.context is not required; do our best to construct one
                    traverseLogger.debug('{}: @odata.context missing from Registry resource; constructed context {}'
                                         .format(acquiredtype, context))
                else:
                    traverseLogger.error('{}:  Json does not contain @odata.context'.format(uri))
            if isComplex:
                context = createContext(acquiredtype)

        self.context = context

        # Get Schema object
        self.schemaObj = rfSchema.getSchemaObject(acquiredtype, self.context)

        if self.schemaObj is None:
            traverseLogger.error("ResourceObject creation: No schema XML for {} {} {}".format(typename, acquiredtype, self.context))
            raise ValueError

        # Use string comprehension to get highest type
        if acquiredtype is typename:
            acquiredtype = self.schemaObj.getHighestType(typename)
            if not isComplex:
                traverseLogger.warning(
                    'No @odata.type present, assuming highest type {}'.format(typename))

        # Check if we provide a valid type (todo: regex)
        self.typename = acquiredtype
        typename = self.typename

        self.initiated = True

        # get our metadata
        metadata = currentService.metadata if currentService else None

        idtag = (typename, context)
        if idtag in ResourceObj.robjcache:
            self.typeobj = ResourceObj.robjcache[idtag]
        else:
            self.typeobj = PropType(
                typename, self.schemaObj, topVersion=getNamespace(typename))

        self.propertyList = self.typeobj.getProperties(self.jsondata)
        propertyList = [prop.propChild for prop in self.propertyList]


        # get additional
        self.additionalList = []
        propTypeObj = self.typeobj
        if propTypeObj.propPattern is not None and len(propTypeObj.propPattern) > 0:
            prop_pattern = propTypeObj.propPattern.get('Pattern', '.*')
            prop_type = propTypeObj.propPattern.get('Type','Resource.OemObject')

            regex = re.compile(prop_pattern)
            for key in [k for k in self.jsondata if k not in propertyList and regex.match(k)]:
                val = self.jsondata.get(key)
                value_obj = PropItem(propTypeObj.schemaObj, propTypeObj.fulltype, key, val, customType=prop_type)
                self.additionalList.append(value_obj)

        # get annotation
        successService, annotationProps = getAnnotations(metadata, self.jsondata)
        if successService:
            self.additionalList.extend(annotationProps)

        # list illegitimate properties together
        self.unknownProperties = [k for k in self.jsondata if k not in propertyList +
                [prop.propChild  for prop in self.additionalList] and '@odata' not in k]

        self.links = OrderedDict()

        oem = config.get('oemcheck', True)
        sample = config.get('sample')
        linklimits = config.get('linklimits', {})
        self.links.update(self.typeobj.getLinksFromType(self.jsondata, self.context, self.propertyList, oem, linklimits, sample))

        self.links.update(getAllLinks(
            self.jsondata, self.additionalList, self.schemaObj, context=context, linklimits=linklimits,
            sample_size=sample, oemCheck=oem))

    def getResourceProperties(self):
        allprops = self.propertyList + self.additionalList[:min(len(self.additionalList), 100)]
        return allprops

class PropItem:
    def __init__(self, schemaObj, propOwner, propChild, val, topVersion=None, customType=None):
        try:
            self.name = propOwner + ':' + propChild
            self.propOwner, self.propChild = propOwner, propChild
            self.propDict = getPropertyDetails(
                schemaObj, propOwner, propChild, val, topVersion, customType)
            self.attr = self.propDict['attrs']
        except Exception:
            traverseLogger.debug('Exception caught while creating new PropItem', exc_info=1)
            traverseLogger.error(
                    '{}:{} :  Could not get details on this property'.format(str(propOwner),str(propChild)))
            self.propDict = None
            return
        pass

class PropAction:
    def __init__(self, propOwner, propChild, act):
        try:
            self.name = '#{}.{}'.format(propOwner, propChild)
            self.propOwner, self.propChild = propOwner, propChild
            self.actTag = act
        except Exception:
            traverseLogger.debug('Exception caught while creating new PropAction', exc_info=1)
            traverseLogger.error(
                    '{}:{} :  Could not get details on this action'.format(str(propOwner),str(propChild)))
            self.actTag = None


class PropType:
    def __init__(self, typename, schemaObj, topVersion=None):
        # if we've generated this type, use it, else generate type
        self.initiated = False
        self.fulltype = typename
        self.schemaObj = schemaObj
        self.snamespace, self.stype = getNamespace(
            self.fulltype), getType(self.fulltype)
        self.additional = False

        self.isNav = False
        self.propList = []
        self.actionList = []
        self.parent = None
        self.propPattern = None

        # get all properties and actions in Type chain
        success, currentSchemaObj, baseType = True, self.schemaObj, self.fulltype
        try:
            newPropList, newActionList, self.additional, self.propPattern = getTypeDetails(
                currentSchemaObj, baseType, topVersion)

            self.propList.extend(newPropList)
            self.actionList.extend(newActionList)

            success, currentSchemaObj, baseType = currentSchemaObj.getParentType(baseType)
            if success:
                self.parent = PropType(
                    baseType, currentSchemaObj, topVersion=topVersion)
                if not self.additional:
                    self.additional = self.parent.additional
        except Exception as ex:
            traverseLogger.debug('Exception caught while creating new PropType', exc_info=1)
            traverseLogger.error(
                '{}:  Getting type failed for {}'.format(str(self.fulltype), str(baseType)))
            raise ex

        self.initiated = True

    def getTypeChain(self):
        if self.fulltype is None:
            raise StopIteration
        else:
            node = self
            tlist = []
            while node is not None:
                tlist.append(node.fulltype)
                yield node.fulltype
                node = node.parent
            raise StopIteration

    def getLinksFromType(self, jsondata, context, propList=None, oemCheck=True, linklimits={}, sample=None):
        node = self
        links = OrderedDict()
        while node is not None:
            links.update(getAllLinks(jsondata, node.getProperties(jsondata) if propList is None else propList, node.schemaObj, context=context, linklimits=linklimits, sample_size=sample, oemCheck=oemCheck))
            node = node.parent
        return links

    def getProperties(self, jsondata):
        node = self
        props = []
        while node is not None:
            for prop in node.propList:
                schemaObj, newPropOwner, newProp, topVersion = prop
                val = jsondata.get(newProp)
                props.append(PropItem(schemaObj, newPropOwner, newProp, val, topVersion=topVersion))
            node = node.parent
        return props

    def getActions(self):
        node = self
        while node is not None:
            for prop in node.actionList:
                yield prop
            node = node.parent
        raise StopIteration


def getTypeDetails(schemaObj, SchemaAlias, topVersion=None):
    # spits out information on the type we have, prone to issues if references/soup is ungettable, this shouldn't be ran without it
    #   has been prone to a lot of confusing errors: rehaul information that user expects to know before this point is reached
    # info: works undercover, but maybe can point out what type was generated and how many properties were found, if additional props allowed...
    # debug: all typegen info
    # error: if we're missing something, otherwise should be find getting all properties and letting them handle their own generation.
    #   if something can't be genned, let that particular property (PropItem) handle itself, no catches
    """
    Gets list of surface level properties for a given SchemaType,
    """
    PropertyList = list()
    ActionList = list()
    PropertyPattern = None
    additional = False

    soup, refs = schemaObj.soup, schemaObj.refs

    SchemaNamespace, SchemaType = getNamespace(
        SchemaAlias), getType(SchemaAlias)

    traverseLogger.debug("Generating type: {}".format(SchemaAlias))
    traverseLogger.debug("Schema is {}, {}".format(
                        SchemaType, SchemaNamespace))

    innerschema = soup.find('Schema', attrs={'Namespace': SchemaNamespace})

    if innerschema is None:
        uri = schemaObj.origin
        traverseLogger.error('Schema namespace {} not found in schema file {}. Will not be able to gather type details.'
                             .format(SchemaNamespace, uri))
        return PropertyList, ActionList, False, PropertyPattern

    element = innerschema.find(['EntityType', 'ComplexType'], attrs={'Name': SchemaType}, recursive=False)

    if element is None:
        uri = schemaObj.origin
        traverseLogger.error('Element {} not found in schema namespace {}. Will not be able to gather type details.'
                             .format(SchemaType, SchemaNamespace))
        return PropertyList, ActionList, False, PropertyPattern

    traverseLogger.debug("___")
    traverseLogger.debug(element.get('Name'))
    traverseLogger.debug(element.attrs)
    traverseLogger.debug(element.get('BaseType'))

    additionalElement = element.find(
        'Annotation', attrs={'Term': 'OData.AdditionalProperties'})
    additionalElementOther = element.find(
        'Annotation', attrs={'Term': 'Redfish.DynamicPropertyPatterns'})
    if additionalElement is not None:
        additional = additionalElement.get('Bool', False)
        if additional in ['false', 'False', False]:
            additional = False
        if additional in ['true', 'True']:
            additional = True
    else:
        additional = False
    if additionalElementOther is not None:
        # create PropertyPattern dict containing pattern and type for DynamicPropertyPatterns validation
        traverseLogger.debug('getTypeDetails: Redfish.DynamicPropertyPatterns found, element = {}, SchemaAlias = {}'
                             .format(element, SchemaAlias))
        pattern_elem = additionalElementOther.find("PropertyValue", Property="Pattern")
        pattern = prop_type = None
        if pattern_elem is not None:
            pattern = pattern_elem.get("String")
        type_elem = additionalElementOther.find("PropertyValue", Property="Type")
        if type_elem is not None:
            prop_type = type_elem.get("String")
        traverseLogger.debug('getTypeDetails: pattern = {}, type = {}'.format(pattern, prop_type))
        if pattern is not None and prop_type is not None:
            PropertyPattern = dict()
            PropertyPattern['Pattern'] = pattern
            PropertyPattern['Type'] = prop_type
        additional = True

    # get properties
    usableProperties = element.find_all(['NavigationProperty', 'Property'], recursive=False)

    for innerelement in usableProperties:
        traverseLogger.debug(innerelement['Name'])
        traverseLogger.debug(innerelement.get('Type'))
        traverseLogger.debug(innerelement.attrs)
        newPropOwner = SchemaAlias if SchemaAlias is not None else 'SomeSchema'
        newProp = innerelement['Name']
        traverseLogger.debug("ADDING :::: {}:{}".format(newPropOwner, newProp))
        PropertyList.append(
             (schemaObj, newPropOwner, newProp, topVersion))

    # get actions
    usableActions = innerschema.find_all(['Action'], recursive=False)

    for act in usableActions:
        newPropOwner = getNamespace(SchemaAlias) if SchemaAlias is not None else 'SomeSchema'
        newProp = act['Name']
        traverseLogger.debug("ADDING ACTION :::: {}:{}".format(newPropOwner, newProp))
        ActionList.append(
             PropAction(newPropOwner, newProp, act))

    return PropertyList, ActionList, additional, PropertyPattern


def getPropertyDetails(schemaObj, propertyOwner, propertyName, val, topVersion=None, customType=None):
    """
    Get dictionary of tag attributes for properties given, including basetypes.

    param arg1: soup data
    param arg2: references
    ...
    """

    propEntry = dict()
    propEntry['val'] = val
    OwnerNamespace, OwnerType = getNamespace(propertyOwner), getType(propertyOwner)
    traverseLogger.debug('___')
    traverseLogger.debug('{}, {}:{}'.format(OwnerNamespace, propertyOwner, propertyName))

    soup, refs = schemaObj.soup, schemaObj.refs

    if customType is None:
        # Get Schema of the Owner that owns this prop
        ownerSchema = soup.find('Schema', attrs={'Namespace': OwnerNamespace})

        if ownerSchema is None:
            traverseLogger.warning(
                "getPropertyDetails: Schema could not be acquired,  {}".format(OwnerNamespace))
            return None

        # Get Entity of Owner, then the property of the Property we're targeting
        ownerEntity = ownerSchema.find(
            ['EntityType', 'ComplexType'], attrs={'Name': OwnerType}, recursive=False)  # BS4 line

        # check if this property is a nav property
        # Checks if this prop is an annotation
        success, propertySoup, propertyRefs, propertyFullType = True, soup, refs, OwnerType

        if '@' not in propertyName:
            propEntry['isTerm'] = False  # not an @ annotation
            propertyTag = ownerEntity.find(
                ['NavigationProperty', 'Property'], attrs={'Name': propertyName}, recursive=False)  # BS4 line

            # start adding attrs and props together
            propertyInnerTags = propertyTag.find_all()  # BS4 line
            for tag in propertyInnerTags:
                propEntry[tag['Term']] = tag.attrs
            propertyFullType = propertyTag.get('Type')
        else:
            propEntry['isTerm'] = True
            ownerEntity = ownerSchema.find(
                ['Term'], attrs={'Name': OwnerType}, recursive=False)  # BS4 line
            if ownerEntity is None:
                ownerEntity = ownerSchema.find(
                    ['EntityType', 'ComplexType'], attrs={'Name': OwnerType}, recursive=False)  # BS4 line
            propertyTag = ownerEntity
            propertyFullType = propertyTag.get('Type', propertyOwner)

        propEntry['isNav'] = propertyTag.name == 'NavigationProperty'
        propEntry['attrs'] = propertyTag.attrs
        traverseLogger.debug(propEntry)

        propEntry['realtype'] = 'none'

    else:
        propertyFullType = customType
        propEntry['realtype'] = 'none'
        propEntry['attrs'] = dict()
        propEntry['attrs']['Type'] = customType
        metadata = currentService.metadata
        serviceRefs = currentService.metadata.get_service_refs()
        serviceSchemaSoup = currentService.metadata.get_soup()
        success, propertySoup, propertyRefs, propertyFullType = True, serviceSchemaSoup, serviceRefs, customType

    # find the real type of this, by inheritance
    while propertyFullType is not None:
        traverseLogger.debug("HASTYPE")
        PropertyNamespace, PropertyType = getNamespace(propertyFullType), getType(propertyFullType)

        traverseLogger.debug('{}, {}'.format(PropertyNamespace, propertyFullType))

        # Type='Collection(Edm.String)'
        # If collection, check its inside type
        if re.match('Collection\(.*\)', propertyFullType) is not None:
            if val is not None and not isinstance(val, list):
                raise TypeError('This collection is not a List')
            propertyFullType = propertyFullType.replace('Collection(', "").replace(')', "")
            propEntry['isCollection'] = propertyFullType
            continue
        else:
            if val is not None and isinstance(val, list) and propEntry.get('isCollection') is None :
                raise TypeError('This item should not be a List')

        # If basic, just pass itself
        if 'Edm' in propertyFullType:
            propEntry['realtype'] = propertyFullType
            break

        # get proper soup, check if this Namespace is the same as its Owner, otherwise find its SchemaXml
        if PropertyNamespace.split('.')[0] != OwnerNamespace.split('.')[0]:
            schemaObj = schemaObj.getSchemaFromReference(PropertyNamespace)
            success = schemaObj is not None
            if success:
                propertySoup = schemaObj.soup
                propertyRefs = schemaObj.refs
        else:
            success, propertySoup, uri = True, soup, 'of parent'

        if not success:
            traverseLogger.warning(
                "getPropertyDetails: Could not acquire appropriate Schema for this item, {} {} {}".format(propertyOwner, PropertyNamespace, propertyName))
            return propEntry

        # traverse tags to find the type
        propertySchema = propertySoup.find(
            'Schema', attrs={'Namespace': PropertyNamespace})
        if propertySchema is None:
            traverseLogger.warning('Schema element with Namespace attribute of {} not found in schema file {}'
                                 .format(PropertyNamespace, uri))
            break
        propertyTypeTag = propertySchema.find(
            ['EnumType', 'ComplexType', 'EntityType', 'TypeDefinition'], attrs={'Name': PropertyType}, recursive=False)
        nameOfTag = propertyTypeTag.name if propertyTypeTag is not None else 'None'

        # perform more logic for each type
        if nameOfTag == 'TypeDefinition': # Basic type
            # This piece of code is rather simple UNLESS this is an "enumeration"
            #   this is a unique deprecated enum, labeled as Edm.String

            propertyFullType = propertyTypeTag.get('UnderlyingType')
            isEnum = propertyTypeTag.find(  # BS4 line
                'Annotation', attrs={'Term': 'Redfish.Enumeration'}, recursive=False)

            if propertyFullType == 'Edm.String' and isEnum is not None:
                propEntry['realtype'] = 'deprecatedEnum'
                propEntry['typeprops'] = list()
                memberList = isEnum.find(  # BS4 line
                    'Collection').find_all('PropertyValue')  # BS4 line

                for member in memberList:
                    propEntry['typeprops'].append(member.get('String'))
                traverseLogger.debug("{}".format(propEntry['typeprops']))
                break
            else:
                continue

        elif nameOfTag == 'ComplexType': # go deeper into this type
            traverseLogger.debug("go deeper in type")

            # We need to find the highest existence of this type vs topVersion schema
            # not ideal, but works for this solution
            success, baseSoup, baseRefs, baseType = True, propertySoup, propertyRefs, propertyFullType

            # If we're outside of our normal Soup, then do something different, otherwise elif
            if PropertyNamespace.split('.')[0] != OwnerNamespace.split('.')[0] and not customType:
                typelist = []
                schlist = []
                for schema in baseSoup.find_all('Schema'):
                    if schema.find('ComplexType', attrs={'Name': PropertyType}) is None:
                        continue
                    newNamespace = schema.get('Namespace')
                    typelist.append(newNamespace)
                    schlist.append(schema)
                for item, schema in reversed(sorted(zip(typelist, schlist))):
                    traverseLogger.debug(
                        "Working backwards: {}   {}".format(item, getType(baseType)))
                    baseType = item + '.' + getType(baseType)
                    break
            elif topVersion is not None and (topVersion != OwnerNamespace):
                currentVersion = topVersion
                currentSchema = baseSoup.find(  # BS4 line
                    'Schema', attrs={'Namespace': currentVersion})
                # Working backwards from topVersion schematag,
                #   created expectedType, check if currentTypeTag exists
                #   if it does, use our new expectedType, else continue down parent types
                #   until we exhaust all schematags in file
                while currentSchema is not None:
                    expectedType = currentVersion + '.' + PropertyType
                    currentTypeTag = currentSchema.find(  # BS4 line
                        'ComplexType', attrs={'Name': PropertyType})
                    if currentTypeTag is not None:
                        baseType = expectedType
                        traverseLogger.debug('new type: ' + baseType)  # Printout FORMAT
                        break
                    else:
                        nextEntity = currentSchema.find(  # BS4 line
                            ['EntityType', 'ComplexType'], attrs={'Name': OwnerType})
                        nextType = nextEntity.get('BaseType')
                        currentVersion = getNamespace(nextType)
                        currentSchema = baseSoup.find(  # BS4 line
                            'Schema', attrs={'Namespace': currentVersion})
                        continue
            propEntry['realtype'] = 'complex'
            if propEntry.get('isCollection') is None:
                propEntry['typeprops'] = createResourceObject(propertyName, 'complex', val, context=schemaObj.context, typename=baseType, isComplex=True)
            else:
                val = val if val is not None else {}
                propEntry['typeprops'] = [createResourceObject(propertyName, 'complex', item, context=schemaObj.context, typename=baseType, isComplex=True) for item in val]
            break

        elif nameOfTag == 'EnumType': # If enum, get all members
            propEntry['realtype'] = 'enum'
            propEntry['typeprops'] = list()
            for MemberName in propertyTypeTag.find_all('Member'):  # BS4 line
                propEntry['typeprops'].append(MemberName['Name'])
            break

        elif nameOfTag == 'EntityType': # If entity, do nothing special (it's a reference link)
            propEntry['realtype'] = 'entity'
            propEntry['typeprops'] = dict()
            traverseLogger.debug("typeEntityTag found {}".format(propertyTypeTag['Name']))
            break

        else:
            traverseLogger.error('Type {} not found under namespace {} in schema {}'
                                 .format(PropertyType, PropertyNamespace, uri))
            break

    return propEntry


def enumerate_collection(items, cTypeName, linklimits, sample_size):
    """
    Generator function to enumerate the items in a collection, applying the link limit or sample size if applicable.
    If a link limit is specified for this cTypeName, return the first N items as specified by the limit value.
    If a sample size greater than zero is specified, return a random sample of items specified by the sample_size.
    In both the above cases, if the limit value or sample size is greater than or equal to the number of items in the
    collection, return all the items.
    If a limit value for this cTypeName and a sample size are both provided, the limit value takes precedence.
    :param items: the collection of items to enumerate
    :param cTypeName: the type name of this collection
    :param linklimits: a dictionary mapping type names to their limit values
    :param sample_size: the number of items to sample from large collections
    :return: enumeration of the items to be processed
    """
    if cTypeName in linklimits:
        # "link limit" case
        limit = min(linklimits[cTypeName], len(items))
        traverseLogger.debug('Limiting "{}" to first {} links'.format(cTypeName, limit))
        for i in range(limit):
            if linklimits[cTypeName] < len(items):
                uri = items[i].get('@odata.id')
                if uri is not None:
                    uri_sample_map[uri] = 'Collection limit {} of {}'.format(i + 1, limit)
            yield i, items[i]
    elif 0 < sample_size < len(items):
        # "sample size" case
        traverseLogger.debug('Limiting "{}" to sample of {} links'.format(cTypeName, sample_size))
        sample = 0
        for i in sorted(random.sample(range(len(items)), sample_size)):
            sample += 1
            uri = items[i].get('@odata.id')
            if uri is not None:
                uri_sample_map[uri] = 'Collection sample {} of {}'.format(sample, sample_size)
            yield i, items[i]
    else:
        # "all" case
        traverseLogger.debug('Processing all links for "{}"'.format(cTypeName))
        yield from enumerate(items)


def getAllLinks(jsonData, propList, schemaObj, prefix='', context='', linklimits=None, sample_size=0, oemCheck=True):
    """
    Function that returns all links provided in a given JSON response.
    This result will include a link to itself.

    :param arg1: json dict
    :param arg2: property dict
    :param arg3: reference dict
    :param prefix: default blank, for deeper links
    :param context: default blank, for AutoExpanded types
    :return: list of links
    """
    linkList = OrderedDict()
    if linklimits is None:
        linklimits = {}
    # check keys in propertyDictionary
    # if it is a Nav property, check that it exists
    #   if it is not a Nav Collection, add it to list
    #   otherwise, add everything IN Nav collection
    # if it is a Complex property, check that it exists
    #   if it is, recurse on collection or individual item
    if not isinstance(jsonData, dict):
        traverseLogger.error("Generating links requires a dict")
    refDict = schemaObj.refs
    try:
        for propx in propList:
            propDict = propx.propDict
            if propDict is None:
                continue

            isNav = propDict.get('isNav', False)
            key = propx.name
            item = getType(key).split(':')[-1]

            insideItem = jsonData.get(item)
            autoExpand = propDict.get('OData.AutoExpand', None) is not None or\
                propDict.get('OData.AutoExpand'.lower(), None) is not None
            cType = propDict.get('isCollection')
            ownerNS = propx.propOwner.split('.')[0]
            ownerType = propx.propOwner.split('.')[-1]

            if isNav:
                if insideItem is not None:
                    if cType is not None:
                        cTypeName = getType(cType)
                        cSchema = refDict.get(getNamespace(cType), (None, None))[1]
                        if cSchema is None:
                            cSchema = context
                        for cnt, listItem in enumerate_collection(insideItem, cTypeName, linklimits, sample_size):
                            linkList[prefix + str(item) + '.' + cTypeName +
                                     '#' + str(cnt)] = (listItem.get('@odata.id'), autoExpand, cType, cSchema, listItem)
                    else:
                        cType = propDict['attrs'].get('Type')
                        cSchema = refDict.get(getNamespace(cType), (None, None))[1]
                        if cSchema is None:
                            cSchema = context
                        linkList[prefix + str(item) + '.' + getType(propDict['attrs']['Name'])] = (
                            insideItem.get('@odata.id'), autoExpand, cType, cSchema, insideItem)
            elif item == 'Uri' and ownerNS == 'MessageRegistryFile' and ownerType == 'Location':
                # special handling for MessageRegistryFile Location Uri
                if insideItem is not None and isinstance(insideItem, str) and len(insideItem) > 0:
                    uriItem = {'@odata.id': insideItem}
                    cType = ownerNS + '.' + ownerNS
                    cSchema = refDict.get(getNamespace(cType), (None, None))[1]
                    if cSchema is None:
                        cSchema = context
                    traverseLogger.debug('Registry Location Uri: resource = {}, type = {}, schema = {}'
                                         .format(insideItem, cType, cSchema))
                    linkList[prefix + str(item) + '.' + getType(propDict['attrs']['Name'])] = (
                        uriItem.get('@odata.id'), autoExpand, cType, cSchema, uriItem)
            elif item == 'Actions':
                # special handling for @Redfish.ActionInfo payload annotations
                if isinstance(insideItem, dict):
                    cType = 'ActionInfo.ActionInfo'
                    cSchema = refDict.get(getNamespace(cType), (None, None))[1]
                    for k, v in insideItem.items():
                        if not isinstance(v, dict):
                            continue
                        uri = v.get('@Redfish.ActionInfo')
                        if isinstance(uri, str):
                            uriItem = {'@odata.id': uri}
                            traverseLogger.debug('{}{}: @Redfish.ActionInfo annotation uri = {}'.format(item, k, uri))
                            linkList[prefix + str(item) + k + '.' + cType] = (
                                uriItem.get('@odata.id'), autoExpand, cType, cSchema, uriItem)

        for propx in propList:
            propDict = propx.propDict
            if propDict is None:
                continue
            propDict = propx.propDict
            key = propx.name
            item = getType(key).split(':')[-1]
            if 'Oem' in item and not oemCheck:
                continue
            cType = propDict.get('isCollection')
            if propDict is None:
                continue
            elif propDict['realtype'] == 'complex':
                tp = propDict['typeprops']
                if jsonData.get(item) is not None and tp is not None:
                    if cType is not None:
                        cTypeName = getType(cType)
                        for item in tp:
                            linkList.update(item.links)
                    else:
                        linkList.update(tp.links)
        traverseLogger.debug(str(linkList))
    except Exception:
        traverseLogger.debug('Exception caught while getting all links', exc_info=1)
    # contents of Registries may be needed to validate other resources (like Bios), so move to front of linkList
    if 'Registries.Registries' in linkList:
        linkList.move_to_end('Registries.Registries', last=False)
        traverseLogger.debug('getAllLinks: Moved Registries.Registries to front of list')
    return linkList


def getAnnotations(metadata, decoded, prefix=''):
    """
    Function to gather @ additional props in a payload
    """
    allowed_annotations = ['odata', 'Redfish', 'Privileges', 'Message']
    if metadata is not None:
        schemaObj = metadata.schema_obj
    else:
        traverseLogger.warn("Cannot work on annotations without a service or metadata")
        return False, []
    additionalProps = list()
    # For every ...@ in decoded, check for its presence in refs
    #   get the schema file for it
    #   concat type info together
    annotationsFound = 0
    for key in [k for k in decoded if prefix + '@' in k and '@odata' not in k]:
        annotationsFound += 1
        splitKey = key.split('@', 1)
        fullItem = splitKey[1]
        if getNamespace(fullItem) not in allowed_annotations:
            traverseLogger.error("getAnnotations: {} is not an allowed annotation namespace, please check spelling/capitalization.".format(fullItem))
            continue
        elif metadata is not None:
            # add the namespace to the set of namespaces referenced by this service
            metadata.add_service_namespace(getNamespace(fullItem))
        annotationSchemaObj = schemaObj.getSchemaFromReference(getNamespace(fullItem))
        traverseLogger.debug('{}, {}, {}'.format(key, splitKey, decoded[key]))
        if annotationSchemaObj is not None:
            realType = annotationSchemaObj.name
            realItem = realType + '.' + fullItem.split('.', 1)[1]
            additionalProps.append(
                PropItem(annotationSchemaObj, realItem, key, decoded[key]))
    traverseLogger.debug("Annotations generated: {} out of {}".format(len(additionalProps), annotationsFound))
    return True, additionalProps
