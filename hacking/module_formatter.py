#!/usr/bin/env python
# (c) 2012, Jan-Piet Mens <jpmens () gmail.com>
#
# This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.
#

import os
import glob
import sys
import yaml
import codecs
import json
import ast
import re
import optparse
import time
import datetime
import subprocess
import cgi
from jinja2 import Environment, FileSystemLoader

import ansible.utils
import ansible.utils.module_docs as module_docs

#####################################################################################
# constants and paths

# if a module is added in a version of Ansible older than this, don't print the version added information
# in the module documentation because everyone is assumed to be running something newer than this already.
TO_OLD_TO_BE_NOTABLE = 1.0

# Get parent directory of the directory this script lives in
MODULEDIR=os.path.abspath(os.path.join(
    os.path.dirname(os.path.realpath(__file__)), os.pardir, 'library'
))

# The name of the DOCUMENTATION template
EXAMPLE_YAML=os.path.abspath(os.path.join(
    os.path.dirname(os.path.realpath(__file__)), os.pardir, 'examples', 'DOCUMENTATION.yml'
))

_ITALIC = re.compile(r"I\(([^)]+)\)")
_BOLD   = re.compile(r"B\(([^)]+)\)")
_MODULE = re.compile(r"M\(([^)]+)\)")
_URL    = re.compile(r"U\(([^)]+)\)")
_CONST  = re.compile(r"C\(([^)]+)\)")

#####################################################################################

def rst_ify(text):
    ''' convert symbols like I(this is in italics) to valid restructured text '''

    t = _ITALIC.sub(r'*' + r"\1" + r"*", text)
    t = _BOLD.sub(r'**' + r"\1" + r"**", t)
    t = _MODULE.sub(r'``' + r"\1" + r"``", t)
    t = _URL.sub(r"\1", t)
    t = _CONST.sub(r'``' + r"\1" + r"``", t)

    return t

#####################################################################################

def html_ify(text):
    ''' convert symbols like I(this is in italics) to valid HTML '''

    t = cgi.escape(text)
    t = _ITALIC.sub("<em>" + r"\1" + "</em>", t)
    t = _BOLD.sub("<b>" + r"\1" + "</b>", t)
    t = _MODULE.sub("<span class='module'>" + r"\1" + "</span>", t)
    t = _URL.sub("<a href='" + r"\1" + "'>" + r"\1" + "</a>", t)
    t = _CONST.sub("<code>" + r"\1" + "</code>", t)

    return t


#####################################################################################

def rst_fmt(text, fmt):
    ''' helper for Jinja2 to do format strings '''

    return fmt % (text)

#####################################################################################

def rst_xline(width, char="="):
    ''' return a restructured text line of a given length '''

    return char * width

#####################################################################################

def write_data(text, options, outputname, module):
    ''' dumps module output to a file or the screen, as requested '''

    if options.output_dir is not None:
        f = open(os.path.join(options.output_dir, outputname % module), 'w')
        f.write(text.encode('utf-8'))
        f.close()
    else:
        print text

#####################################################################################

def boilerplate():
    ''' prints the boilerplate for module docs '''

    if not os.path.exists(EXAMPLE_YAML):
        print >>sys.stderr, "Missing example boiler plate: %s" % EXAMPLE_YAML
    print "DOCUMENTATION = '''"
    print file(EXAMPLE_YAML).read()
    print "'''"
    print ""
    print ""
    print "EXAMPLES = '''"
    print "# example of doing ___ from a playbook"
    print "your_module: some_arg=1 other_arg=2"
    print "'''"
    print ""

#####################################################################################

def list_modules(module_dir):
    ''' returns a hash of categories, each category being a hash of module names to file paths '''

    categories = {}
    files = glob.glob("%s/*" % module_dir)
    for d in files:
        if os.path.isdir(d):
            files2 = glob.glob("%s/*" % d)
            for f in files2:
                tokens = f.split("/")
                module = tokens[-1]
                category = tokens[-2]
                if not category in categories:
                    categories[category] = {}
                categories[category][module] = f
    return categories

#####################################################################################

def generate_parser():
    ''' generate an optparse parser '''

    p = optparse.OptionParser(
        version='%prog 1.0',
        usage='usage: %prog [options] arg1 arg2',
        description='Generate module documentation from metadata',
    )

    p.add_option("-A", "--ansible-version", action="store", dest="ansible_version", default="unknown", help="Ansible version number")
    p.add_option("-M", "--module-dir", action="store", dest="module_dir", default=MODULEDIR, help="Ansible library path")
    p.add_option("-T", "--template-dir", action="store", dest="template_dir", default="hacking/templates", help="directory containing Jinja2 templates")
    p.add_option("-t", "--type", action='store', dest='type', choices=['html', 'latex', 'man', 'rst', 'json', 'markdown', 'js'], default='latex', help="Document type")
    p.add_option("-v", "--verbose", action='store_true', default=False, help="Verbose") 
    p.add_option("-o", "--output-dir", action="store", dest="output_dir", default=None, help="Output directory for module files")
    p.add_option("-I", "--includes-file", action="store", dest="includes_file", default=None, help="Create a file containing list of processed modules")
    p.add_option("-G", "--generate", action="store_true", dest="do_boilerplate", default=False, help="generate boilerplate docs to stdout")
    p.add_option('-V', action='version', help='Show version number and exit')
    return p

#####################################################################################

def jinja2_environment(template_dir, typ):

    env = Environment(loader=FileSystemLoader(template_dir),
        variable_start_string="@{",
        variable_end_string="}@",
        trim_blocks=True,
    )
    env.globals['xline'] = rst_xline

    if typ == 'rst':
        env.filters['convert_symbols_to_format'] = rst_ify
        env.filters['html_ify'] = html_ify
        env.filters['fmt'] = rst_fmt
        env.filters['xline'] = rst_xline
        template = env.get_template('rst.j2')
        outputname = "%s_module.rst"
    else:
        raise Exception("unknown module format type: %s" % typ)

    return env, template, outputname

#####################################################################################

def process_module(module, options, env, template, outputname, module_map):

    print "rendering: %s" % module

    fname = module_map[module]

    # ignore files with extensions
    if os.path.basename(fname).find(".") != -1:
        return

    # use ansible core library to parse out doc metadata YAML and plaintext examples
    doc, examples = ansible.utils.module_docs.get_docstring(fname, verbose=options.verbose)

    # crash if module is missing documentation and not explicitly hidden from docs index
    if doc is None and module not in ansible.utils.module_docs.BLACKLIST_MODULES:
        sys.stderr.write("*** ERROR: CORE MODULE MISSING DOCUMENTATION: %s, %s ***\n" % (fname, module))
        sys.exit(1)
    if doc is None:
        return "SKIPPED"

    all_keys = []

    if not 'version_added' in doc:
        sys.stderr.write("*** ERROR: missing version_added in: %s ***\n" % module)
        sys.exit(1)

    added = 0
    if doc['version_added'] == 'historical':
        del doc['version_added']
    else:
        added = doc['version_added']

    # don't show version added information if it's too old to be called out
    if added:
        added_tokens = str(added).split(".")
        added = added_tokens[0] + "." + added_tokens[1]
        added_float = float(added)
        if added and added_float < TO_OLD_TO_BE_NOTABLE:
            del doc['version_added']

    for (k,v) in doc['options'].iteritems():
        all_keys.append(k)
    all_keys = sorted(all_keys)
    doc['option_keys'] = all_keys

    doc['filename']         = fname
    doc['docuri']           = doc['module'].replace('_', '-')
    doc['now_date']         = datetime.date.today().strftime('%Y-%m-%d')
    doc['ansible_version']  = options.ansible_version
    doc['plainexamples']    = examples  #plain text

    # here is where we build the table of contents...

    text = template.render(doc)
    write_data(text, options, outputname, module)

#####################################################################################

def process_category(category, categories, options, env, template, outputname):

    module_map = categories[category]

    category_file_path = os.path.join(options.output_dir, "list_of_%s_modules.rst" % category)
    category_file = open(category_file_path, "w")
    print "*** recording category %s in %s ***" % (category, category_file_path) 

    # TODO: start a new category file

    category = category.replace("_"," ")
    category = category.title()

    modules = module_map.keys()
    modules.sort()

    category_header = "%s Modules" % (category.title())
    underscores = "`" * len(category_header)

    category_file.write(category_header)
    category_file.write("\n")
    category_file.write(underscores)
    category_file.write("\n")
    category_file.write(".. toctree::\n")

    for module in modules:
        result = process_module(module, options, env, template, outputname, module_map)
        if result != "SKIPPED":
            category_file.write("    %s_module\n" % module)


    category_file.close()

    # TODO: end a new category file

#####################################################################################

def validate_options(options):
    ''' validate option parser options '''

    if options.do_boilerplate:
        boilerplate()
        sys.exit(0)

    if not options.module_dir:
        print >>sys.stderr, "--module-dir is required"
        sys.exit(1)
    if not os.path.exists(options.module_dir):
        print >>sys.stderr, "--module-dir does not exist: %s" % options.module_dir
        sys.exit(1)
    if not options.template_dir:
        print "--template-dir must be specified"
        sys.exit(1)

#####################################################################################

def main():

    p = generate_parser()

    (options, args) = p.parse_args()
    validate_options(options)

    env, template, outputname = jinja2_environment(options.template_dir, options.type)

    categories = list_modules(options.module_dir)
    last_category = None
    category_names = categories.keys()
    category_names.sort()
    
    category_list_path = os.path.join(options.output_dir, "modules_by_category.rst")
    category_list_file = open(category_list_path, "w")
    category_list_file.write("Module Index\n")
    category_list_file.write("============\n")
    category_list_file.write("\n\n")
    category_list_file.write(".. toctree::\n")
 
    for category in category_names:
        category_list_file.write("    list_of_%s_modules\n" % category)
        process_category(category, categories, options, env, template, outputname)

    category_list_file.close()

if __name__ == '__main__':
    main()
