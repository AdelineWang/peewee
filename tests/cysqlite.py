import os

from peewee import *
from playhouse._cysqlite_ext import *

from .base import DatabaseTestCase


database = CySqliteExtDatabase('peewee_test.db', timeout=0.1, hash_functions=1)


class CyDatabaseTestCase(DatabaseTestCase):
    database = database

    def tearDown(self):
        super(CyDatabaseTestCase, self).tearDown()
        if os.path.exists(self.database.database):
            os.unlink(self.database.database)

    def execute(self, sql, *params):
        return self.database.execute_sql(sql, params, commit=False)


class TestCySqliteHelpers(CyDatabaseTestCase):
    def test_autocommit(self):
        self.assertTrue(self.database.autocommit)
        self.database.begin()
        self.assertFalse(self.database.autocommit)
        self.database.rollback()
        self.assertTrue(self.database.autocommit)

    def test_commit_hook(self):
        state = {}

        @self.database.on_commit
        def on_commit():
            state.setdefault('commits', 0)
            state['commits'] += 1

        self.execute('create table register (value text)')
        self.assertEqual(state['commits'], 1)

        # Check hook is preserved.
        self.database.close()
        self.database.connect()

        self.execute('insert into register (value) values (?), (?)',
                     'foo', 'bar')
        self.assertEqual(state['commits'], 2)

        curs = self.execute('select * from register order by value;')
        results = curs.fetchall()
        self.assertEqual([tuple(r) for r in results], [('bar',), ('foo',)])

        self.assertEqual(state['commits'], 2)

    def test_rollback_hook(self):
        state = {}

        @self.database.on_rollback
        def on_rollback():
            state.setdefault('rollbacks', 0)
            state['rollbacks'] += 1

        self.execute('create table register (value text);')
        self.assertEqual(state, {})

        # Check hook is preserved.
        self.database.close()
        self.database.connect()

        self.database.begin()
        self.execute('insert into register (value) values (?)', 'test')
        self.database.rollback()
        self.assertEqual(state, {'rollbacks': 1})

        curs = self.execute('select * from register;')
        self.assertEqual(curs.fetchall(), [])

    def test_update_hook(self):
        state = []

        @self.database.on_update
        def on_update(query, db, table, rowid):
            state.append((query, db, table, rowid))

        self.execute('create table register (value text)')
        self.execute('insert into register (value) values (?), (?)',
                     'foo', 'bar')

        self.assertEqual(state, [
            ('INSERT', 'main', 'register', 1),
            ('INSERT', 'main', 'register', 2)])

        # Check hook is preserved.
        self.database.close()
        self.database.connect()

        self.execute('update register set value = ? where rowid = ?', 'baz', 1)
        self.assertEqual(state, [
            ('INSERT', 'main', 'register', 1),
            ('INSERT', 'main', 'register', 2),
            ('UPDATE', 'main', 'register', 1)])

        self.execute('delete from register where rowid=?;', 2)
        self.assertEqual(state, [
            ('INSERT', 'main', 'register', 1),
            ('INSERT', 'main', 'register', 2),
            ('UPDATE', 'main', 'register', 1),
            ('DELETE', 'main', 'register', 2)])

    def test_properties(self):
        mem_used, mem_high = self.database.memory_used
        self.assertTrue(mem_high >= mem_used)
        self.assertFalse(mem_high == 0)

        self.assertTrue(self.database.cache_used is not None)


HUser = Table('users', ('id', 'username'))


class TestHashFunctions(CyDatabaseTestCase):
    database = database

    def setUp(self):
        super(TestHashFunctions, self).setUp()
        self.database.execute_sql(
            'create table users (id integer not null primary key, '
            'username text not null)')

    def test_md5(self):
        for username in ('charlie', 'huey', 'zaizee'):
            HUser.insert({HUser.username: username}).execute(self.database)

        query = (HUser
                 .select(HUser.username,
                         fn.SUBSTR(fn.SHA1(HUser.username), 1, 6).alias('sha'))
                 .order_by(HUser.username)
                 .tuples()
                 .execute(self.database))

        self.assertEqual(query[:], [
            ('charlie', 'd8cd10'),
            ('huey', '89b31a'),
            ('zaizee', 'b4dcf9')])


class TestBackup(CyDatabaseTestCase):
    backup_filename = 'test_backup.db'

    def tearDown(self):
        super(TestBackup, self).tearDown()
        if os.path.exists(self.backup_filename):
            os.unlink(self.backup_filename)

    def test_backup_to_file(self):
        # Populate the database with some test data.
        self.execute('CREATE TABLE register (id INTEGER NOT NULL PRIMARY KEY, '
                     'value INTEGER NOT NULL)')
        with self.database.atomic():
            for i in range(100):
                self.execute('INSERT INTO register (value) VALUES (?)', i)

        self.database.backup_to_file(self.backup_filename)
        backup_db = CySqliteExtDatabase(self.backup_filename)
        cursor = backup_db.execute_sql('SELECT value FROM register ORDER BY '
                                       'value;')
        self.assertEqual([val for val, in cursor.fetchall()], range(100))
        backup_db.close()


class TestBlob(CyDatabaseTestCase):
    def setUp(self):
        super(TestBlob, self).setUp()
        self.Register = Table('register', ('id', 'data'))
        self.execute('CREATE TABLE register (id INTEGER NOT NULL PRIMARY KEY, '
                     'data BLOB NOT NULL)')

    def create_blob_row(self, nbytes):
        Register = self.Register.bind(self.database)
        Register.insert({Register.data: ZeroBlob(nbytes)}).execute()
        return self.database.last_insert_rowid

    def test_blob(self):
        rowid1024 = self.create_blob_row(1024)
        rowid16 = self.create_blob_row(16)

        blob = Blob(self.database, 'register', 'data', rowid1024)
        self.assertEqual(len(blob), 1024)

        blob.write('x' * 1022)
        blob.write('zz')
        blob.seek(1020)
        self.assertEqual(blob.tell(), 1020)

        data = blob.read(3)
        self.assertEqual(data, 'xxz')
        self.assertEqual(blob.read(), 'z')
        self.assertEqual(blob.read(), '')

        blob.seek(-10, 2)
        self.assertEqual(blob.tell(), 1014)
        self.assertEqual(blob.read(), 'xxxxxxxxzz')

        blob.reopen(rowid16)
        self.assertEqual(blob.tell(), 0)
        self.assertEqual(len(blob), 16)

        blob.write('x' * 15)
        self.assertEqual(blob.tell(), 15)

    def test_blob_exceed_size(self):
        rowid = self.create_blob_row(16)

        blob = self.database.blob_open('register', 'data', rowid)
        with self.assertRaisesCtx(ValueError):
            blob.seek(17, 0)

        with self.assertRaisesCtx(ValueError):
            blob.write('x' * 17)

        blob.write('x' * 16)
        self.assertEqual(blob.tell(), 16)
        blob.seek(0)
        data = blob.read(17)  # Attempting to read more data is OK.
        self.assertEqual(data, 'x' * 16)
        blob.close()

    def test_blob_errors_opening(self):
        rowid = self.create_blob_row(4)

        with self.assertRaisesCtx(OperationalError):
            blob = self.database.blob_open('register', 'data', rowid + 1)

        with self.assertRaisesCtx(OperationalError):
            blob = self.database.blob_open('register', 'missing', rowid)

        with self.assertRaisesCtx(OperationalError):
            blob = self.database.blob_open('missing', 'data', rowid)

    def test_blob_operating_on_closed(self):
        rowid = self.create_blob_row(4)
        blob = self.database.blob_open('register', 'data', rowid)
        self.assertEqual(len(blob), 4)
        blob.close()

        with self.assertRaisesCtx(InterfaceError):
            len(blob)

        self.assertRaises(InterfaceError, blob.read)
        self.assertRaises(InterfaceError, blob.write, 'foo')
        self.assertRaises(InterfaceError, blob.seek, 0, 0)
        self.assertRaises(InterfaceError, blob.tell)
        self.assertRaises(InterfaceError, blob.reopen, rowid)

    def test_blob_readonly(self):
        rowid = self.create_blob_row(4)
        blob = self.database.blob_open('register', 'data', rowid)
        blob.write('huey')
        blob.seek(0)
        self.assertEqual(blob.read(), 'huey')
        blob.close()

        blob = self.database.blob_open('register', 'data', rowid, True)
        self.assertEqual(blob.read(), 'huey')
        blob.seek(0)
        with self.assertRaisesCtx(OperationalError):
            blob.write('meow')

        # BLOB is read-only.
        self.assertEqual(blob.read(), 'huey')