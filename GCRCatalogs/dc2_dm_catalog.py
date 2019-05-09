"""
DC2 DM Catalog Reader

Read DC2 catalogs based off LSST Data Management (DM) Science Pipelines output
as extracted and reformatted as Parquet files.
Readers that provide access to DC2 DM data should inherit from this class.
"""

import os
import re
import warnings
import shutil

import numpy as np
import pyarrow.parquet as pq
import yaml

from GCR import BaseGenericCatalog

from abc import ABCMeta

__all__ = ['DC2DMCatalog']


#pylint: disable=C0103
def convert_flux_to_nanoJansky(flux, fluxmag0):
    """Convert the listed DM coadd-reported flux values to nanoJansky.

    Based on the given fluxmag0 value, which is AB mag = 0.
    Eventually we will get nJy from the final calibrated DRP processing.
    """
    #pylint: disable=C0103
    AB_mag_zp_wrt_Jansky = 8.90  # Definition of AB
    # 9 is from nano=10**(-9)
    #pylint: disable=C0103
    AB_mag_zp_wrt_nanoJansky = 2.5 * 9 + AB_mag_zp_wrt_Jansky

    return 10**((AB_mag_zp_wrt_nanoJansky)/2.5) * flux / fluxmag0


def create_basic_flag_mask(*flags):
    """Generate a mask for a set of flags

    For each item the mask will be true if and only if all flags are false

    Args:
        *flags (ndarray): Variable number of arrays with booleans or equivalent

    Returns:
        The combined mask array
    """

    out = np.ones(len(flags[0]), np.bool)
    for flag in flags:
        out &= (~flag)

    return out


class DC2DMCatalog(BaseGenericCatalog, metaclass=ABCMeta):
    r"""DC2 Catalog reader

    Parameters
    ----------
    base_dir          (str): Directory of data files being served, required
    filename_pattern  (str): The optional regex pattern of served data files
    use_cache        (bool): Cache read data in memory
    is_dpdd          (bool): File are already in DPDD-format.  No translation.

    Attributes
    ----------
    base_dir          (str): The directory of data files being served
    """
    # pylint: disable=too-many-instance-attributes

    FILE_DIR = os.path.dirname(os.path.abspath(__file__))
    FILE_PATTERN = r'source_visit_\d+\.parquet$'
    SCHEMA_FILENAME = 'schema.yaml'
    META_PATH = os.path.join(FILE_DIR, 'catalog_configs/_dc2_source_meta.yaml')

    def _subclass_init(self, **kwargs):
        self.base_dir = kwargs['base_dir']
        self._filename_re = re.compile(kwargs.get('filename_pattern', self.FILE_PATTERN))
        self.use_cache = bool(kwargs.get('use_cache', True))

        if not os.path.isdir(self.base_dir):
            raise ValueError('`base_dir` {} is not a valid directory'.format(self.base_dir))

        _schema_filename = kwargs.get('schema_filename', self.SCHEMA_FILENAME)
        # If _schema_filename is an absolute path, os.path.join will just return _schema_filename
        self._schema_path = os.path.join(self.base_dir, _schema_filename)

        self._schema = None
        if self._schema_path and os.path.isfile(self._schema_path):
            self._schema = self._generate_schema_from_yaml(self._schema_path)

        self._file_handles = dict()
        self._datasets = self._generate_datasets()
        if not self._datasets:
            err_msg = 'No catalogs were found in `base_dir` {}'
            raise RuntimeError(err_msg.format(self.base_dir))

        if not self._schema:
            warnings.warn('Falling back to reading all datafiles for column names')
            self._schema = self._generate_schema_from_datafiles(self._datasets)

        if kwargs.get('is_dpdd'):
            self._quantity_modifiers = {col: None for col in self._schema}
        else:
            if any(col.endswith('_fluxSigma') for col in self._schema):
                dm_schema_version = 1
            elif any(col.endswith('_fluxErr') for col in self._schema):
                dm_schema_version = 2
            else:
                dm_schema_version = 3

            self._quantity_modifiers = self._generate_modifiers(dm_schema_version)

        self._quantity_info_dict = self._generate_info_dict(self.META_PATH)
        self._native_filter_quantities = self._generate_native_quantity_list()

    def __del__(self):
        self.close_all_file_handles()

    @staticmethod
    def _generate_modifiers(dm_schema_version=3):  # pylint: disable=unused-argument
        """Creates a dictionary relating native and homogenized column names

        Args:
            dm_schema_version (int): DM schema version (1, 2, or 3)

        Returns:
            A dictionary of the form {<homogenized name>: <native name>, ...}
        """
        return dict()

    @staticmethod
    def _generate_info_dict(meta_path):
        """Creates a 2d dictionary with information for each homogenized quantity

        Args:
            meta_path (path): Path of yaml config file with object meta data

        Returns:
            Dictionary of the form
                {<homonogized value (str)>: {<meta value (str)>: <meta data>}, ...}
        """

        with open(meta_path, 'r') as ofile:
            base_dict = yaml.load(ofile)

        info_dict = dict()
        for quantity, info_list in base_dict.items():
            quantity_info = dict(
                description=info_list[0],
                unit=info_list[1],
                in_GCRbase=info_list[2],
                in_DPDD=info_list[3]
            )
            info_dict[quantity] = quantity_info

        return info_dict

    def _get_quantity_info_dict(self, quantity, default=None):
        """Return a dictionary with descriptive information for a quantity

        Returned information includes a quantity description, quantity units,
        whether the quantity is defined in the DPDD,
        and if the quantity is available in GCRbase.

        Args:
            quantity   (str): The quantity to return information for
            default (object): Value to return if no information is available (default None)

        Returns:
            A dictionary with information about the provided quantity
        """

        return self._quantity_info_dict.get(quantity, default)

    def _generate_datasets(self):
        """Return viable data sets from all files in self.base_dir

        Returns:
            A list of ObjectTableWrapper(<file path>, <key>) objects
            for all files and keys
        """
        datasets = list()
        for fname in sorted(os.listdir(self.base_dir)):
            if not self._filename_re.match(fname):
                continue

            file_path = os.path.join(self.base_dir, fname)
            try:
                df = self._open_parquet(file_path)

            except (IOError, OSError):
                warnings.warn('Cannot access {}; skipped'.format(file_path))
                continue

            datasets.append(df)

        return datasets

    @staticmethod
    def _generate_schema_from_yaml(schema_path):
        """Return a dictionary of columns based on schema in YAML file

        Args:
            schema_path (string): <file path> to schema file.

        Returns:
            The columns defined in the schema.
            A dictionary of {<column_name>: {'dtype': ..., 'default': ...}, ...}

        Warns:
            If one or more column names are repeated.
        """

        with open(schema_path, 'r') as schema_stream:
            schema = yaml.load(schema_stream)

        if schema is None:
            warn_msg = 'No schema can be found in schema file {}'
            warnings.warn(warn_msg.format(schema_path))

        return schema

    @staticmethod
    def _generate_schema_from_datafiles(datasets):
        """Return the native schema for given datasets

        Args:
            datasets (list): A list of tuples (<file path>, <key>)

        Returns:
            A dict of schema ({col_name: {'dtype': dtype}}) found in all data sets
        """

        schema = {}
        for dataset in datasets:
            # I should be able to do this just from the ParquetFile schema
            # but that's a bit clunky and I don'tk now quite how to write it out
            df = dataset.read().to_pandas()
            # Reformat k, v as k: {'dtype': v} because that's our chosen schema format
            native_schema = {k: {'dtype': v} for k, v in df.dtypes.to_dict().items()}
            schema.update(native_schema)
            # The first non-empty one will be fine.
            if native_schema:
                break

        return schema

    def generate_schema_yaml(self, overwrite=False):
        """
        Generate the schema from the datafiles and write as a yaml file.
        This function write the schema yaml file to the schema location specified for the catalog.
        One needs to set `overwrite=True` to overwrite an existing schema file.
        """
        if self._schema_path and os.path.isfile(self._schema_path):
            if not overwrite:
                raise RuntimeError('Schema file `{}` already exists! Set `overwrite=True` to overwrite.'.format(self._schema_path))
            warnings.warn('Overwriting schema file `{0}`, which is backed up at `{0}.bak`'.format(self._schema_path))
            shutil.copyfile(self._schema_path, self._schema_path + '.bak')

        schema = self._generate_schema_from_datafiles(self._datasets)

        for col, schema_this in schema.items():
            if schema_this['dtype'] == 'bool' and (
                    col.endswith('_flag_bad') or col.endswith('_flag_noGoodPixels')):
                schema_this['default'] = True

        with open(self._schema_path, 'w') as schema_stream:
            yaml.dump(schema, schema_stream)

    def clear_cache(self):
        """Empty the catalog reader cache and frees up memory allocation"""

        for dataset in self._datasets:
            dataset.clear_cache()

    def _open_parquet(self, file_path):
        """Return the Parquet filehandle for a Parquet file

        Args:
            file_path (str): The path of the desired file

        Return:
            The cached file handle
        """
        if file_path not in self._file_handles:
            self._file_handles[file_path] = pq.ParquetFile(file_path)

        return self._file_handles[file_path]

    def close_all_file_handles(self):
        """Clear all cached file handles"""

        for fh in self._file_handles.values():
            del fh

        self._file_handles.clear()

    def _generate_native_quantity_list(self):
        """Return a set of native quantity names as strings"""
        return set(self._schema)

    def _iter_native_dataset(self, native_filters=None):
        # pylint: disable=C0330
        for dataset in self._datasets:
            if native_filters is None:
                def native_quantity_getter(native_quantity, dataset=dataset):
                    data = dataset.read(columns=[native_quantity])
                    return data.to_pandas()[native_quantity].values

                yield native_quantity_getter
                if not self.use_cache:
                    dataset.clear_cache()