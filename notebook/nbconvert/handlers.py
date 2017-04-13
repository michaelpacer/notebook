"""Tornado handlers for nbconvert."""

# Copyright (c) Jupyter Development Team.
# Distributed under the terms of the Modified BSD License.

import io
import os
import json
import zipfile

from tornado import web, escape
from tornado.log import app_log

from ..base.handlers import (
    IPythonHandler, FilesRedirectHandler,
    path_regex,
)
from nbformat import from_dict
import nbformat
from traitlets.config import Config

from ipython_genutils.py3compat import cast_bytes
from ipython_genutils import text

def find_resource_files(output_files_dir):
    files = []
    for dirpath, dirnames, filenames in os.walk(output_files_dir):
        files.extend([os.path.join(dirpath, f) for f in filenames])
    return files

def respond_zip(handler, name, output, resources):
    """Zip up the output and resource files and respond with the zip file.

    Returns True if it has served a zip file, False if there are no resource
    files, in which case we serve the plain output file.
    """
    # Check if we have resource files we need to zip
    output_files = resources.get('outputs', None)
    if not output_files:
        return False
    
    # Headers
    zip_filename = os.path.splitext(name)[0] + '.zip'
    handler.set_header('Content-Disposition',
                       'attachment; filename="%s"' % escape.url_escape(zip_filename))
    handler.set_header('Content-Type', 'application/zip')
    
    # create zip file 
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode='w', compression=zipfile.ZIP_DEFLATED) as zipf:
        output_filename = os.path.splitext(name)[0] + resources['output_extension']
        zipf.writestr(output_filename, cast_bytes(output, 'utf-8'))
        # add external resources
        for filename, data in output_files.items():
            zipf.writestr(filename, data)
    
    handler.finish(buffer.getvalue())
    return True

def get_exporter(format, **kwargs):
    """get an exporter, raising appropriate errors"""
    # if this fails, will raise 500
    try:
        from nbconvert.exporters.base import get_exporter
    except ImportError as e:
        raise web.HTTPError(500, "Could not import nbconvert: %s" % e)

    try:
        Exporter = get_exporter(format)
    except KeyError:
        # should this be 400?
        raise web.HTTPError(404, u"No exporter for format: %s" % format)

    try:
        return Exporter(**kwargs)
    except Exception as e:
        app_log.exception("Could not construct Exporter: %s", Exporter)
        raise web.HTTPError(500, "Could not construct Exporter: %s" % e)

class NbconvertFileHandler(IPythonHandler):

    def call_nbconvert(self, format, path, config=None, content=None, post=False):


        exporter = get_exporter(format, config=config, log=self.log)
        path = path.strip('/')
        # If the notebook relates to a real file (default contents manager),
        # give its path to nbconvert.
        if hasattr(self.contents_manager, '_get_os_path'):
            os_path = self.contents_manager._get_os_path(path)
            ext_resources_dir, basename = os.path.split(os_path)
        else:
            ext_resources_dir = None

        model = self.contents_manager.get(path=path)
        name = model['name']
        if model['type'] != 'notebook':
            # not a notebook, redirect to files
            return FilesRedirectHandler.redirect_to_files(self, path)

        if content is None:
            nb = model['content']
        else:
            nb = nbformat.reads(content, as_version=4)

        self.set_header('Last-Modified', model['last_modified'])

        # create resources dictionary
        mod_date = model['last_modified'].strftime(text.date_format)
        nb_title = os.path.splitext(name)[0]

        resource_dict = {
            "metadata": {
                "name": nb_title,
                "modified_date": mod_date
            },
            "config_dir": self.application.settings['config_dir'],
            "output_files_dir": nb_title+"_files",
        }

        if ext_resources_dir:
            resource_dict['metadata']['path'] = ext_resources_dir

        try:
            output, resources = exporter.from_notebook_node(
                nb,
                resources=resource_dict
            )
        except Exception as e:
            self.log.exception("nbconvert failed: %s", e)
            raise web.HTTPError(500, "nbconvert failed: %s" % e)

        if respond_zip(self, name, output, resources):
            return

        # Force download if requested
        if self.get_argument('download', 'false').lower() == 'true':
            filename = os.path.splitext(name)[0] + resources['output_extension']
            self.set_attachment_header(filename)

        # MIME type
        if exporter.output_mimetype:
            self.set_header('Content-Type',
                            '%s; charset=utf-8' % exporter.output_mimetype)

        self.finish(output)

    @web.authenticated
    def get(self, format, path):

        self.call_nbconvert(format, path, config=self.config)

    @web.authenticated
    def post(self, format, path):
        c = Config(self.config)
        json_upload = self.get_json_body()
        json_config = json.loads(json_upload.get("config",{}))
        c.merge(json_config)
        nb_content = json.dumps(json_upload["notebook"])
        self.call_nbconvert(format, path, config=c, content=nb_content, post=True)


class NbconvertPostHandler(IPythonHandler):
    SUPPORTED_METHODS = ('POST',)

    @web.authenticated
    def post(self, format):
        exporter = get_exporter(format, config=self.config)

        model = self.get_json_body()
        name = model.get('name', 'notebook.ipynb')
        nbnode = from_dict(model['content'])

        try:
            output, resources = exporter.from_notebook_node(nbnode, resources={
                "metadata": {"name": name[:name.rfind('.')],},
                "config_dir": self.application.settings['config_dir'],
            })
        except Exception as e:
            raise web.HTTPError(500, "nbconvert failed: %s" % e)

        if respond_zip(self, name, output, resources):
            return

        # MIME type
        if exporter.output_mimetype:
            self.set_header('Content-Type',
                            '%s; charset=utf-8' % exporter.output_mimetype)

        self.finish(output)


#-----------------------------------------------------------------------------
# URL to handler mappings
#-----------------------------------------------------------------------------

_format_regex = r"(?P<format>\w+)"


default_handlers = [
    (r"/nbconvert/%s" % _format_regex, NbconvertPostHandler),
    (r"/nbconvert/%s%s" % (_format_regex, path_regex),
         NbconvertFileHandler),
]
