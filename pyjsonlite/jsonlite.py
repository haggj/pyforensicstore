# Copyright (c) 2019 Siemens AG
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of
# this software and associated documentation files (the "Software"), to deal in
# the Software without restriction, including without limitation the rights to
# use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
# the Software, and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS
# FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
# COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
# IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
#
# Author(s): Jonas Plum

"""
JSONLite is a database that can be used to store items and files.

"""
import hashlib
import logging
import sqlite3
import uuid
from contextlib import contextmanager
from typing import Any
import os.path

import jsonschema
import flatten_json
from flatten_json import flatten, unflatten_list
from fs import path, open_fs, errors, base

from .flatten_monkey import unflatten
from .hashed_file import HashedFile

flatten_json.unflatten = unflatten

LOGGER = logging.getLogger(__name__)

DISCRIMINATOR = "type"


def open_fs_file(location: str, create: bool = False) -> (base.FS, str):
    if isinstance(location, tuple):
        return location[0], location[1]

    filename = path.basename(location)
    try:
        file_system = open_fs(
            location[:-len(filename)], create=create)  # type: base.FS
    except errors.CreateFailed as error:
        raise RuntimeError("Could not create %s (%s)" % (location, error))
    return file_system, filename


class JSONLite:
    """
    JSONLite is a class to database that can be used to store items and files.

    :param str remote_url: Location of the database. Needs to be a path or a valid pyfilesystem2 url
    """

    db_file = "item.db"

    def __init__(self, remote_url: str):
        if isinstance(remote_url, str):
            if remote_url[-1] == "/":
                remote_url = remote_url[:-1]
            self.remote_fs = open_fs(remote_url, create=True)
        else:
            self.remote_fs = remote_url

        self.new = not self.remote_fs.exists(self.db_file)

        dbpath = path.join(self.remote_fs.getsyspath("."), self.db_file)
        self.connection = sqlite3.connect(dbpath, timeout=10.0)
        self.connection.row_factory = sqlite3.Row

        self._schemas = dict()
        self._tables = self._get_tables()

    ################################
    #   API
    ################################

    def insert(self, item: dict) -> str:
        """
        Insert a single item into the store

        :param dict item: New item
        :return: ID if the inserted item
        :rtype: int
        """
        if DISCRIMINATOR not in item:
            raise KeyError("Missing discriminator %s in item" % DISCRIMINATOR)
        # add uuid
        if 'uid' in item:
            item['id'] = item['uid']
            del item['uid']
        if 'id' not in item:
            item['id'] = item[DISCRIMINATOR] + '--' + str(uuid.uuid4())

        # discard empty values
        item = {k: v for k, v in item.items() if v is not None and not (isinstance(v, list) and not v)}

        validation_errors = self.validate_item_schema(item)
        if validation_errors:
            raise TypeError("item could not be validated", validation_errors)

        item['uid'] = item['id']
        del item['id']

        column_names, column_values, flat_item = self._flatten_item(item)

        self._ensure_table(column_names, flat_item, item)

        # insert item
        cur = self.connection.cursor()
        query = "INSERT INTO \"{table}\" ({columns}) VALUES ({values})".format(
            table=item[DISCRIMINATOR],
            columns=", ".join(['"' + c + '"' for c in column_names]),
            values=", ".join(['?'] * len(column_values))
        )
        LOGGER.debug("insert query: %s", query)
        try:
            cur.execute(query, column_values)
        except sqlite3.InterfaceError as error:
            print(query, column_values)
            raise error
        finally:
            cur.close()

        return item['uid']

    def get(self, item_id: str) -> dict:
        """
        Get a single item by the item_id

        :param str item_id: ID of the item
        :return: Single item
        :rtype: dict
        """
        cur = self.connection.cursor()

        discriminator, _, _ = item_id.partition("--")

        try:
            cur.execute(
                "SELECT * FROM \"{table}\" WHERE uid=?".format(table=discriminator), (item_id,))
            result = cur.fetchone()
            if not result:
                raise KeyError("Item does not exist")

            return self._row_to_item(result)
        except sqlite3.OperationalError as error:
            raise KeyError(error)
        finally:
            cur.close()

    def query(self, query: str) -> []:
        cur = self.connection.cursor()
        cur.execute(query)
        for row in cur.fetchall():
            yield self._row_to_item(row)
        cur.close()

    def update(self, item_id: str, partial_item: dict) -> str:
        """
        Update a single item

        :param str item_id: ID of the item
        :param dict partial_item: Changes for the item
        """
        cur = self.connection.cursor()

        updated_item = self.get(item_id)
        old_discriminator = updated_item[DISCRIMINATOR]
        updated_item.update(partial_item)

        _, _, item_uuid = item_id.partition("--")

        # type changed
        if DISCRIMINATOR in partial_item and old_discriminator != partial_item[DISCRIMINATOR]:
            updated_item["uid"] = partial_item[DISCRIMINATOR] + \
                                  '--' + item_uuid
            cur.execute("DELETE FROM \"{table}\" WHERE uid=?".format(
                table=old_discriminator), [item_id])
            return self.insert(updated_item)

        column_names, _, flat_item = self._flatten_item(updated_item)

        self._ensure_table(column_names, flat_item, updated_item)

        values = []
        replacements = []
        for key, value in flat_item.items():
            replacements.append("\"%s\"=?" % key)
            values.append(value)
        replace = ", ".join(replacements)

        values.append(item_id)
        table = updated_item[DISCRIMINATOR]
        cur.execute("UPDATE \"{table}\" SET {replace} WHERE uid=?".format(
            table=table, replace=replace), values)
        cur.close()

        return updated_item["id"]

    def import_jsonlite(self, url: str):
        """
        Import jsonlite file

        :param str url: Location of the observed data file. Needs to be a path or a valid pyfilesystem2 url
        """
        import_db = connect(url)
        for item in import_db.all():
            self._import_file(import_db.remote_fs, item)

    def _import_file(self, file_system, item: dict):
        for field in item:
            if field.endswith("_path"):
                with self.store_file(item[field]) as (file_path, file):
                    file.write(file_system.readbytes(item[field]))
                item.update({field: file_path})
        self.insert(item)

    @contextmanager
    def store_file(self, file_path: str) -> (str, HashedFile):
        """
        Creates a writeable context for the contents of the file.

        :param str file_path: Relative location of the new file
        :return: A file object with a .write method
        :rtype: HashedFile
        """
        self.remote_fs.makedirs(path.dirname(file_path), recreate=True)
        i = 0
        base_path, ext = path.splitext(file_path)
        while self.remote_fs.exists(file_path):
            file_path = "%s_%d%s" % (base_path, i, ext)
            i += 1

        the_file = HashedFile(file_path, self.remote_fs)
        yield (file_path, the_file)
        the_file.close()

    @contextmanager
    def load_file(self, file_path: str):
        the_file = self.remote_fs.open(file_path)
        yield the_file
        the_file.close()

    def close(self):
        """
        Save ForensicStore to its location.
        """
        self.connection.commit()
        self.connection.close()

    ################################
    #   Validate
    ################################

    def validate(self):
        validation_errors = []
        expected_files = set()

        expected_files.add('/' + path.basename(self.db_file))

        for item in self.all():
            # validate item
            item_errors, item_expected_files = self.validate_item(item)
            validation_errors.extend(item_errors)
            expected_files |= item_expected_files

        stored_files = set({f for f in self.remote_fs.walk.files() if not f.endswith(
            '/' + path.basename(self.db_file) + "-journal")})

        if expected_files - stored_files:
            validation_errors.append("missing files: ('%s')" % "', '".join(expected_files - stored_files))
        if stored_files - expected_files:
            validation_errors.append("additional files: ('%s')" % "', '".join(stored_files - expected_files))

        return validation_errors

    def validate_item(self, item: dict):
        """
        Validate a single item

        :param dict item: Item for validation
        :raises TypeError: If item is invalid
        """
        validation_errors = []
        expected_files = set()

        if DISCRIMINATOR not in item:
            validation_errors.append("Item needs to have a discriminator, got %s" % item)

        validation_errors += self.validate_item_schema(item)

        # collect export paths
        for field in item.keys():
            if field.endswith("_path"):
                export_path = item[field]

                # validate parent paths
                if '..' in export_path:
                    validation_errors.append("'..' in %s" % export_path)
                    continue

                expected_files.add('/' + export_path)

                # validate existence, is validated later as well
                if not self.remote_fs.exists(item[field]):
                    continue

                # validate size
                if "size" in item:
                    if item["size"] != self.remote_fs.getsize(export_path):
                        validation_errors.append("wrong size for %s" % export_path)

                if "hashes" in item:
                    for hash_algorithm_name, value in item["hashes"].items():
                        if hash_algorithm_name == "MD5":
                            hash_algorithm = hashlib.md5()
                        elif hash_algorithm_name == "SHA-1":
                            hash_algorithm = hashlib.sha1()
                        else:
                            validation_errors.append("unsupported hash %s for %s" % (hash_algorithm_name, export_path))
                            continue
                        hash_algorithm.update(self.remote_fs.readbytes(export_path))
                        if hash_algorithm.hexdigest() != value:
                            validation_errors.append(
                                "hashvalue mismatch %s for %s" % (hash_algorithm_name, export_path)
                            )

        return validation_errors, expected_files

    def jsonlite_handler(self, uri):
        return self._schema(uri)

    def validate_item_schema(self, item):
        validation_errors = []

        item_type = item[DISCRIMINATOR]
        schema = self._schema(item_type)
        if schema is None:
            return validation_errors

        try:
            jsonschema.validate(item, schema, resolver=JSONLiteResolver(self, item_type))
        except jsonschema.ValidationError as error:
            validation_errors.append("Item could not be validated, %s" % str(error))
        return validation_errors

    def select(self, item_type: str, conditions=None) -> []:
        """
        Select items from the ForensicStore

        :param str item_type: Type of the items
        :param [dict] conditions: List of key values pairs. Items matching any list element are returned
        :return: Item generator with the results
        :rtype: [dict]
        """
        if conditions is None:
            conditions = []

        # query db
        ors = []
        for condition in conditions:
            ands = []
            for key, value in condition.items():
                if key != "type":
                    ands.append("\"%s\" LIKE \"%s\"" % (key, value))
            if ands:
                ors.append("(" + " AND ".join(ands) + ")")

        cur = self.connection.cursor()
        query = "SELECT * FROM \"{table}\"".format(table=item_type)
        if ors:
            query += " WHERE %s" % " OR ".join(ors)

        rows = []
        LOGGER.debug("select query: %s", query)
        try:
            cur.execute(query)
            rows = cur.fetchall()
        except sqlite3.OperationalError as error:
            if "no such table" not in str(error):
                raise error
        finally:
            cur.close()

        for row in rows:
            yield self._row_to_item(row)

    def all(self) -> []:
        """
        Get all items with any time from the ForensicStore
        :return: Item generator with the results
        :rtype: [dict]
        """
        cur = self.connection.cursor()
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE '%sqlite%';")
        tables = cur.fetchall()

        for table_name in tables:
            table_name = table_name["name"]
            if not table_name.startswith("_"):
                cur.execute(
                    "SELECT * FROM \"{table}\"".format(table=table_name))
                for row in cur.fetchall():
                    yield self._row_to_item(row)
        cur.close()

    ################################
    #   Intern
    ################################

    @staticmethod
    def _flatten_item(item: dict) -> ([], [], dict):
        # flatten item and discard empty lists
        flat_item = flatten(item, '.')
        column_names = []
        column_values = []
        for key, value in flat_item.items():
            if not isinstance(value, list) or (isinstance(value, list) and value):
                column_names.append(key)
                column_values.append(value)

        return column_names, column_values, flat_item

    @staticmethod
    def _row_to_item(row) -> dict:
        clean_result = dict()
        for k in row.keys():
            if row[k] is not None:
                clean_result[k] = row[k]

        clean_result['id'] = clean_result['uid']
        del clean_result['uid']

        return unflatten_list(clean_result, '.')

    def _get_tables(self) -> dict:
        cur = self.connection.cursor()
        cur.execute("SELECT name FROM sqlite_master")

        tables = {}
        for table in cur.fetchall():
            tables[table['name']] = {}
            cur.execute("PRAGMA table_info (\"{table}\")".format(
                table=table['name']))
            for col in cur.fetchall():
                tables[table['name']][col["name"]] = col["type"]
        cur.close()

        return tables

    def _ensure_table(self, column_names: [], flat_item: dict, item: dict):
        # create table if not exits
        if item[DISCRIMINATOR] not in self._tables:
            self._create_table(column_names, flat_item)
        # add missing columns
        else:
            missing_columns = set(flat_item.keys()) - \
                              set(self._tables[item[DISCRIMINATOR]])
            if missing_columns:
                self._add_missing_columns(
                    item[DISCRIMINATOR], flat_item, missing_columns)

    def _create_table(self, column_names: [], flat_item: dict):
        self._tables[flat_item[DISCRIMINATOR]] = {
            'uid': 'TEXT', DISCRIMINATOR: 'TEXT'
        }
        columns = "uid TEXT PRIMARY KEY, %s TEXT NOT NULL" % DISCRIMINATOR
        for column in column_names:
            if column not in [DISCRIMINATOR, 'uid']:
                sql_data_type = self._get_sql_data_type(flat_item[column])
                self._tables[flat_item[DISCRIMINATOR]][column] = sql_data_type
                columns += ", \"{column}\" {sql_data_type}".format(
                    column=column, sql_data_type=sql_data_type
                )
        cur = self.connection.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS \"{table}\" ({columns})".format(
            table=flat_item[DISCRIMINATOR], columns=columns
        ))
        cur.close()

    def _add_missing_columns(self, table: str, columns: dict, new_columns: []):
        cur = self.connection.cursor()
        # add missing columns
        for new_column in new_columns:
            sql_data_type = self._get_sql_data_type(columns[new_column])
            self._tables[table][new_column] = sql_data_type
            cur.execute("ALTER TABLE \"{table}\" ADD COLUMN \"{column}\" {sql_data_type}".format(
                table=table, column=new_column, sql_data_type=sql_data_type
            ))
        cur.close()

    @staticmethod
    def _get_sql_data_type(value: Any):
        if isinstance(value, int):
            return "INTEGER"
        return "TEXT"

    def _set_schema(self, name: str, schema: Any):
        if name in self._schemas and self._schemas[name] == schema:
            return
        self._schemas[name] = schema

    def _schema(self, name: str) -> Any:
        if name in self._schemas:
            return self._schemas[name]
        return None

    def getinfo(self, item_path, namespaces=None):
        """ Get info regarding a file or directory. """
        return self.remote_fs.getinfo(item_path, namespaces)

    def listdir(self, item_path):
        """ Get a list of resources in a directory. """
        return self.remote_fs.listdir(item_path)

    def makedir(self, item_path, permissions=None, recreate=False):
        """ Make a directory. """
        return self.remote_fs.makedir(item_path, permissions, recreate)

    def openbin(self, item_path, mode=u'r', buffering=-1, **options):
        """ Open a binary file. """
        return self.remote_fs.openbin(item_path, mode, buffering, **options)

    def remove(self, item_path):
        """ Remove a file. """
        return self.remote_fs.remove(item_path)

    def removedir(self, item_path):
        """ Remove a directory. """
        return self.remote_fs.removedir(item_path)

    def setinfo(self, item_path, info):
        """ Set resource information. """
        return self.remote_fs.setinfo(item_path, info)


def connect(url: str) -> JSONLite:
    return JSONLite(url)


class JSONLiteResolver:

    def __init__(self, jsonlite: JSONLite, item_type):
        self.jsonlite = jsonlite
        self.scope = [item_type]

    def push_scope(self, scope):
        self.scope.append(scope.replace("jsonlite:", ""))

    def pop_scope(self):
        self.scope.pop()

    def resolve(self, ref):
        if not ref.startswith("#"):
            basename, _ = os.path.splitext(os.path.basename(ref))
            document = self.jsonlite._schema(basename) # pylint: disable=protected-access
            return ref, document

        # if ref.startswith("jsonlite:"):
        #     document = self.jsonlite._schema(ref.replace("jsonlite:", ""))
        #     return ref, document
        document = self.jsonlite._schema(self.scope[-1])  # pylint: disable=protected-access
        return ref, self.resolve_fragment(document, ref.replace('#', ''))

    @staticmethod
    def resolve_fragment(document, fragment):
        from jsonschema.compat import (Sequence, unquote)
        from jsonschema import (exceptions)

        fragment = fragment.lstrip(u"/")
        parts = unquote(fragment).split(u"/") if fragment else []

        for part in parts:
            part = part.replace(u"~1", u"/").replace(u"~0", u"~")

            if isinstance(document, Sequence):
                # Array indexes should be turned into integers
                try:
                    part = int(part)
                except ValueError:
                    pass
            try:
                document = document[part]
            except (TypeError, LookupError):
                raise exceptions.RefResolutionError(
                    "Unresolvable JSON pointer: %r" % fragment
                )

        return document
