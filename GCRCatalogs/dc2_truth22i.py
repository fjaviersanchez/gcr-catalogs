import os
import warnings
import sqlite3
import numpy as np
import h5py
from GCR import BaseGenericCatalog
from .utils import md5, is_string_like

__all__ = ['DC2Truth22CatalogReader']

class DC2Truth22CatalogReader(BaseGenericCatalog):
    """
    DC2 truth catalog reader for run 2.2i data

    Parameters
    ----------
    filename : str
        path to the sqlite database file
    table_name : str
        table name
    is_static : bool
        whether or not this is for static objects only
    base_filters : str or list of str, optional
        set of filters to always apply to the where clause
    """

    native_filter_string_only = True

    def _subclass_init(self, **kwargs):
        self._filename = kwargs['filename']

        self._table_name = kwargs.get('table_name', 'truth')
        self._is_static = kwargs.get('is_static', True)

        base_filters = kwargs.get('base_filters')
        if base_filters:
            if is_string_like(base_filters):
                self.base_filters = (base_filters,)
            else:
                self.base_filters = tuple(base_filters)
        else:
            self.base_filters = tuple()

        if not os.path.isfile(self._filename):
            raise ValueError('{} is not a valid file'.format(self._filename))

        if kwargs.get('md5') and md5(self._filename) != kwargs['md5']:
            raise ValueError('md5 sum does not match!')

        self._conn = sqlite3.connect(self._filename)

        # get the descriptions of the columns as provided in the sqlite database
        cursor = self._conn.cursor()
        if self._is_static:
            results = cursor.execute('SELECT name, description FROM column_descriptions;')
            self._column_descriptions = dict(results.fetchall())
        else:
            self._column_descriptions = dict()

        results = cursor.execute('PRAGMA table_info({});'.format(self._table_name))
        type_dict = {'BIGINT' : 'int64', 'INT' : 'int32', 'FLOAT' : 'float32',
                     'DOUBLE' : 'float64'}
        self._native_quantity_dtypes = {t[1]: t[2] for t in results.fetchall()}
        for (k,v) in self._native_quantity_dtypes.items():
            #print('key {}   : value {}'.format(k, v))

            if v in type_dict.keys():
                self._native_quantity_dtypes[k] = type_dict[v]

        # if self._is_static:
        #     self._quantity_modifiers = {
        #         'agn': (lambda x: x.astype(np.bool)),
        #         'star': (lambda x: x.astype(np.bool)),
        #         'sprinkled': (lambda x: x.astype(np.bool)),
        #     }

    def _generate_native_quantity_list(self):
        return list(self._native_quantity_dtypes)

    @staticmethod
    def _obtain_native_data_dict(native_quantities_needed, native_quantity_getter):
        """
        Overloading this so that we can query the database backend
        for multiple columns at once
        """
        return native_quantity_getter(native_quantities_needed)

    def _iter_native_dataset(self, native_filters=None):
        cursor = self._conn.cursor()

        if native_filters is not None:
            all_filters = self.base_filters + tuple(native_filters)
        else:
            all_filters = self.base_filters

        if all_filters:
            query_where_clause = 'WHERE ({})'.format(') AND ('.join(all_filters))
        else:
            query_where_clause = ''

        def dc2_truth_native_quantity_getter(quantities):
            # note the API of this getter is not normal, and hence
            # we have overwritten _obtain_native_data_dict
            dtype = np.dtype([(q, self._native_quantity_dtypes[q]) for q in quantities])
            query = 'SELECT {} FROM {} {};'.format(
                ', '.join(quantities),
                self._table_name,
                query_where_clause
            )
            # may need to switch to fetchmany for larger dataset
            return np.array(cursor.execute(query).fetchall(), dtype)

        yield dc2_truth_native_quantity_getter

    def _get_quantity_info_dict(self, quantity, default=None):
        if quantity in self._column_descriptions:
            return {'description': self._column_descriptions[quantity]}
        return default


