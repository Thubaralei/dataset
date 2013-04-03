import logging
from itertools import count

from sqlalchemy.sql import and_, expression
from sqlalchemy.schema import Column, Index

from dataset.persistence.util import guess_type


log = logging.getLogger(__name__)


class Table(object):

    def __init__(self, database, table):
        self.indexes = {}
        self.database = database
        self.table = table

    @property
    def columns(self):
        """
        Get a listing of all columns that exist in the table.

        >>> print 'age' in table.columns
        True
        """
        return set(self.table.columns.keys())

    def drop(self):
        """
        Drop the table from the database, deleting both the schema
        and all the contents within it.

        Note: the object will be in an unusable state after using this
        command and should not be used again. If you want to re-create
        the table, make sure to get a fresh instance from the
        :py:class:`Database <dataset.Database>`.
        """
        with self.database.lock:
            self.database.tables.pop(self.table.name, None)
            self.table.drop(engine)

    def insert(self, row, ensure=True, types={}):
        """
        Add a row (type: dict) by inserting it into the table.
        If ``ensure`` is set, any of the keys of the row are not
        table columns, they will be created automatically.

        During column creation, ``types`` will be checked for a key
        matching the name of a column to be created, and the given
        SQLAlchemy column type will be used. Otherwise, the type is
        guessed from the row value, defaulting to a simple unicode
        field.
        ::

            data = dict(id=10, title='I am a banana!')
            table.insert(data, ['id'])
        """
        if ensure:
            self._ensure_columns(row, types=types)
        self.database.engine.execute(self.table.insert(row))

    def update(self, row, keys, ensure=True, types={}):
        """
        Update a row in the table. The update is managed via
        the set of column names stated in ``keys``: they will be
        used as filters for the data to be updated, using the values
        in ``row``.
        ::

            # update all entries with id matching 10, setting their title columns
            data = dict(id=10, title='I am a banana!')
            table.update(data, ['id'])

        If keys in ``row`` update columns not present in the table,
        they will be created based on the settings of ``ensure`` and
        ``types``, matching the behaviour of :py:meth:`insert() <dataset.Table.insert>`.
        """
        if not len(keys):
            return False
        clause = [(u, row.get(u)) for u in keys]
        if ensure:
            self._ensure_columns(row, types=types)
        try:
            filters = self._args_to_clause(dict(clause))
            stmt = self.table.update(filters, row)
            rp = self.database.engine.execute(stmt)
            return rp.rowcount > 0
        except KeyError:
            return False

    def upsert(self, row, keys, ensure=True, types={}):
        """
        An UPSERT is a smart combination of insert and update. If rows with matching ``keys`` exist
        they will be updated, otherwise a new row is inserted in the table.
        ::

            data = dict(id=10, title='I am a banana!')
            table.upsert(data, ['id'])
        """
        if ensure:
            self.create_index(keys)

        if not self.update(row, keys, ensure=ensure, types=types):
            self.insert(row, ensure=ensure, types=types)

    def delete(self, **filter):
        """ Delete rows from the table. Keyword arguments can be used
        to add column-based filters. The filter criterion will always
        be equality:

        .. code-block:: python

            table.delete(place='Berlin')
        
        If no arguments are given, all records are deleted. 
        """
        q = self._args_to_clause(filter)
        stmt = self.table.delete(q)
        self.database.engine.execute(stmt)

    def _ensure_columns(self, row, types={}):
        for column in set(row.keys()) - set(self.table.columns.keys()):
            if column in types:
                _type = types[column]
            else:
                _type = guess_type(row[column])
            log.debug("Creating column: %s (%s) on %r" % (column,
                                                          _type, self.table.name))
            self.create_column(column, _type)

    def _args_to_clause(self, args):
        self._ensure_columns(args)
        clauses = []
        for k, v in args.items():
            clauses.append(self.table.c[k] == v)
        return and_(*clauses)

    def create_column(self, name, type):
        """
        Explicitely create a new column ``name`` of a specified type.
        ``type`` must be a `SQLAlchemy column type <http://docs.sqlalchemy.org/en/rel_0_8/core/types.html>`_.
        ::

            table.create_column('person', sqlalchemy.String)
        """
        with self.database.lock:
            if name not in self.table.columns.keys():
                col = Column(name, type)
                col.create(self.table,
                           connection=self.database.engine)

    def create_index(self, columns, name=None):
        """
        Create an index to speed up queries on a table. If no ``name`` is given a random name is created.
        ::

            table.create_index(['name', 'country'])
        """
        with self.database.lock:
            if not name:
                sig = abs(hash('||'.join(columns)))
                name = 'ix_%s_%s' % (self.table.name, sig)
            if name in self.indexes:
                return self.indexes[name]
            try:
                columns = [self.table.c[c] for c in columns]
                idx = Index(name, *columns)
                idx.create(self.database.engine)
            except:
                idx = None
            self.indexes[name] = idx
            return idx

    def find_one(self, **filter):
        """
        Works just like :py:meth:`find() <dataset.Table.find>` but returns only one result.
        ::

            row = table.find_one(country='United States')
        """
        res = list(self.find(_limit=1, **filter))
        if not len(res):
            return None
        return res[0]

    def _args_to_order_by(self, order_by):
        if order_by[0] == '-':
            return self.table.c[order_by[1:]].desc()
        else:
            return self.table.c[order_by].asc()

    def find(self, _limit=None, _offset=0, _step=5000,
             order_by='id', **filter):
        """
        Performs a simple search on the table. Simply pass keyword arguments as ``filter``.
        ::

            results = table.find(country='France')
            results = table.find(country='France', year=1980)

        Using ``_limit``::

            # just return the first 10 rows
            results = table.find(country='France', _limit=10)

        You can sort the results by single or multiple columns. Append a minus sign
        to the column name for descending order::

            # sort results by a column 'year'
            results = table.find(country='France', order_by='year')
            # return all rows sorted by multiple columns (by year in descending order)
            results = table.find(order_by=['country', '-year'])

        For more complex queries, please use :py:meth:`db.query() <dataset.Database.query>`
        instead."""
        if isinstance(order_by, (str, unicode)):
            order_by = [order_by]
        order_by = [self._args_to_order_by(o) for o in order_by]

        args = self._args_to_clause(filter)

        for i in count():
            qoffset = _offset + (_step * i)
            qlimit = _step
            if _limit is not None:
                qlimit = min(_limit - (_step * i), _step)
            if qlimit <= 0:
                break
            q = self.table.select(whereclause=args, limit=qlimit,
                                  offset=qoffset, order_by=order_by)
            rows = list(self.database.query(q))
            if not len(rows):
                return
            for row in rows:
                yield row

    def __len__(self):
        """
        Returns the number of rows in the table.
        """
        d = self.database.query(self.table.count()).next()
        return d.values().pop()

    def distinct(self, *columns, **filter):
        """
        Returns all rows of a table, but removes rows in with duplicate values in ``columns``.
        Interally this creates a `DISTINCT statement <http://www.w3schools.com/sql/sql_distinct.asp>`_.
        ::

            # returns only one row per year, ignoring the rest
            table.distinct('year')
            # works with multiple columns, too
            table.distinct('year', 'country')
            # you can also combine this with a filter
            table.distinct('year', country='China')
        """
        qargs = []
        try:
            columns = [self.table.c[c] for c in columns]
            for col, val in filter.items():
                qargs.append(self.table.c[col] == val)
        except KeyError:
            return []

        q = expression.select(columns, distinct=True,
                              whereclause=and_(*qargs),
                              order_by=[c.asc() for c in columns])
        return self.database.query(q)

    def all(self):
        """
        Returns all rows of the table as simple dictionaries. This is simply a shortcut
        to *find()* called with no arguments.
        ::

            rows = table.all()"""
        return self.find()

    def __iter__(self):
        """
        Allows for iterating over all rows in the table without explicetly
        calling :py:meth:`all() <dataset.Table.all>`.
        ::

            for row in table:
                print row
        """
        for row in self.all():
            yield row