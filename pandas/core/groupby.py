import types
from functools import wraps
import numpy as np

from pandas.compat import(
    zip, builtins, range, long, lrange, lzip, OrderedDict, callable
)
from pandas import compat

from pandas.core.base import PandasObject
from pandas.core.categorical import Categorical
from pandas.core.frame import DataFrame
from pandas.core.generic import NDFrame
from pandas.core.index import Index, MultiIndex, _ensure_index
from pandas.core.internals import BlockManager, make_block
from pandas.core.series import Series
from pandas.core.panel import Panel
from pandas.util.decorators import cache_readonly, Appender
import pandas.core.algorithms as algos
import pandas.core.common as com
from pandas.core.common import(_possibly_downcast_to_dtype, isnull,
                               notnull, _DATELIKE_DTYPES, is_numeric_dtype,
                               is_timedelta64_dtype, is_datetime64_dtype)

from pandas import _np_version_under1p7
import pandas.lib as lib
from pandas.lib import Timestamp
import pandas.algos as _algos
import pandas.hashtable as _hash

_agg_doc = """Aggregate using input function or dict of {column -> function}

Parameters
----------
arg : function or dict
    Function to use for aggregating groups. If a function, must either
    work when passed a DataFrame or when passed to DataFrame.apply. If
    passed a dict, the keys must be DataFrame column names.

Notes
-----
Numpy functions mean/median/prod/sum/std/var are special cased so the
default behavior is applying the function along axis=0
(e.g., np.mean(arr_2d, axis=0)) as opposed to
mimicking the default Numpy behavior (e.g., np.mean(arr_2d)).

Returns
-------
aggregated : DataFrame
"""


# special case to prevent duplicate plots when catching exceptions when
# forwarding methods from NDFrames
_plotting_methods = frozenset(['plot', 'boxplot', 'hist'])

_common_apply_whitelist = frozenset([
    'last', 'first',
    'head', 'tail', 'median',
    'mean', 'sum', 'min', 'max',
    'cumsum', 'cumprod', 'cummin', 'cummax', 'cumcount',
    'resample',
    'describe',
    'rank', 'quantile', 'count',
    'fillna',
    'mad',
    'any', 'all',
    'irow', 'take',
    'idxmax', 'idxmin',
    'shift', 'tshift',
    'ffill', 'bfill',
    'pct_change', 'skew',
    'corr', 'cov', 'diff',
]) | _plotting_methods

_series_apply_whitelist = \
    (_common_apply_whitelist - set(['boxplot'])) | \
    frozenset(['dtype', 'value_counts', 'unique', 'nunique'])

_dataframe_apply_whitelist = \
    _common_apply_whitelist | frozenset(['dtypes', 'corrwith'])


class GroupByError(Exception):
    pass


class DataError(GroupByError):
    pass


class SpecificationError(GroupByError):
    pass


def _groupby_function(name, alias, npfunc, numeric_only=True,
                      _convert=False):
    def f(self):
        try:
            return self._cython_agg_general(alias, numeric_only=numeric_only)
        except AssertionError as e:
            raise SpecificationError(str(e))
        except Exception:
            result = self.aggregate(lambda x: npfunc(x, axis=self.axis))
            if _convert:
                result = result.convert_objects()
            return result

    f.__doc__ = "Compute %s of group values" % name
    f.__name__ = name

    return f


def _first_compat(x, axis=0):
    def _first(x):
        x = np.asarray(x)
        x = x[notnull(x)]
        if len(x) == 0:
            return np.nan
        return x[0]

    if isinstance(x, DataFrame):
        return x.apply(_first, axis=axis)
    else:
        return _first(x)


def _last_compat(x, axis=0):
    def _last(x):
        x = np.asarray(x)
        x = x[notnull(x)]
        if len(x) == 0:
            return np.nan
        return x[-1]

    if isinstance(x, DataFrame):
        return x.apply(_last, axis=axis)
    else:
        return _last(x)

class Grouper(object):
    """
    A Grouper allows the user to specify a groupby instruction for a target object

    This specification will select a column via the key parameter, or if the level and/or
    axis parameters are given, a level of the index of the target object.

    These are local specifications and will override 'global' settings, that is the parameters
    axis and level which are passed to the groupby itself.

    Parameters
    ----------
    key : string, defaults to None
        groupby key, which selects the grouping column of the target
    level : name/number, defaults to None
        the level for the target index
    freq : string / freqency object, defaults to None
        This will groupby the specified frequency if the target selection (via key or level) is
        a datetime-like object
    axis : number/name of the axis, defaults to None
    sort : boolean, default to False
        whether to sort the resulting labels

    additional kwargs to control time-like groupers (when freq is passed)

    closed : closed end of interval; left or right
    label : interval boundary to use for labeling; left or right
    convention : {'start', 'end', 'e', 's'}
        If grouper is PeriodIndex

    Returns
    -------
    A specification for a groupby instruction

    Examples
    --------
    >>> df.groupby(Grouper(key='A')) : syntatic sugar for df.groupby('A')
    >>> df.groupby(Grouper(key='date',freq='60s')) : specify a resample on the column 'date'
    >>> df.groupby(Grouper(level='date',freq='60s',axis=1)) :
        specify a resample on the level 'date' on the columns axis with a frequency of 60s

    """

    def __new__(cls, *args, **kwargs):
        if kwargs.get('freq') is not None:
            from pandas.tseries.resample import TimeGrouper
            cls = TimeGrouper
        return super(Grouper, cls).__new__(cls)

    def __init__(self, key=None, level=None, freq=None, axis=None, sort=False):
        self.key=key
        self.level=level
        self.freq=freq
        self.axis=axis
        self.sort=sort

        self.grouper=None
        self.obj=None
        self.indexer=None
        self.binner=None
        self.grouper=None

    @property
    def ax(self):
        return self.grouper

    def _get_grouper(self, obj):

        """
        Parameters
        ----------
        obj : the subject object

        Returns
        -------
        a tuple of binner, grouper, obj (possibly sorted)
        """

        self._set_grouper(obj)
        return self.binner, self.grouper, self.obj

    def _set_grouper(self, obj, sort=False):
        """
        given an object and the specifcations, setup the internal grouper for this particular specification

        Parameters
        ----------
        obj : the subject object

        """

        if self.key is not None and self.level is not None:
            raise ValueError("The Grouper cannot specify both a key and a level!")

        # the key must be a valid info item
        if self.key is not None:
            key = self.key
            if key not in obj._info_axis:
                raise KeyError("The grouper name {0} is not found".format(key))
            ax = Index(obj[key],name=key)

        else:
            ax = obj._get_axis(self.axis)
            if self.level is not None:
                level = self.level

                # if a level is given it must be a mi level or
                # equivalent to the axis name
                if isinstance(ax, MultiIndex):

                    if isinstance(level, compat.string_types):
                        if obj.index.name != level:
                            raise ValueError('level name %s is not the name of the '
                                             'index' % level)
                    elif level > 0:
                        raise ValueError('level > 0 only valid with MultiIndex')
                    ax = Index(ax.get_level_values(level), name=level)

                else:
                    if not (level == 0 or level == ax.name):
                        raise ValueError("The grouper level {0} is not valid".format(level))

            # possibly sort
            if (self.sort or sort) and not ax.is_monotonic:
                indexer = self.indexer = ax.argsort(kind='quicksort')
                ax = ax.take(indexer)
                obj = obj.take(indexer, axis=self.axis, convert=False, is_copy=False)

        self.obj = obj
        self.grouper = ax
        return self.grouper

    def _get_binner_for_grouping(self, obj):
        raise NotImplementedError

    @property
    def groups(self):
        return self.grouper.groups

class GroupBy(PandasObject):

    """
    Class for grouping and aggregating relational data. See aggregate,
    transform, and apply functions on this object.

    It's easiest to use obj.groupby(...) to use GroupBy, but you can also do:

    ::

        grouped = groupby(obj, ...)

    Parameters
    ----------
    obj : pandas object
    axis : int, default 0
    level : int, default None
        Level of MultiIndex
    groupings : list of Grouping objects
        Most users should ignore this
    exclusions : array-like, optional
        List of columns to exclude
    name : string
        Most users should ignore this

    Notes
    -----
    After grouping, see aggregate, apply, and transform functions. Here are
    some other brief notes about usage. When grouping by multiple groups, the
    result index will be a MultiIndex (hierarchical) by default.

    Iteration produces (key, group) tuples, i.e. chunking the data by group. So
    you can write code like:

    ::

        grouped = obj.groupby(keys, axis=axis)
        for key, group in grouped:
            # do something with the data

    Function calls on GroupBy, if not specially implemented, "dispatch" to the
    grouped data. So if you group a DataFrame and wish to invoke the std()
    method on each group, you can simply do:

    ::

        df.groupby(mapper).std()

    rather than

    ::

        df.groupby(mapper).aggregate(np.std)

    You can pass arguments to these "wrapped" functions, too.

    See the online documentation for full exposition on these topics and much
    more

    Returns
    -------
    **Attributes**
    groups : dict
        {group name -> group labels}
    len(grouped) : int
        Number of groups
    """
    _apply_whitelist = _common_apply_whitelist
    _internal_names = ['_cache']
    _internal_names_set = set(_internal_names)

    def __init__(self, obj, keys=None, axis=0, level=None,
                 grouper=None, exclusions=None, selection=None, as_index=True,
                 sort=True, group_keys=True, squeeze=False):
        self._selection = selection

        if isinstance(obj, NDFrame):
            obj._consolidate_inplace()

        self.level = level

        if not as_index:
            if not isinstance(obj, DataFrame):
                raise TypeError('as_index=False only valid with DataFrame')
            if axis != 0:
                raise ValueError('as_index=False only valid for axis=0')

        self.as_index = as_index
        self.keys = keys
        self.sort = sort
        self.group_keys = group_keys
        self.squeeze = squeeze

        if grouper is None:
            grouper, exclusions, obj = _get_grouper(obj, keys, axis=axis,
                                                    level=level, sort=sort)

        self.obj = obj
        self.axis = obj._get_axis_number(axis)
        self.grouper = grouper
        self.exclusions = set(exclusions) if exclusions else set()

    def __len__(self):
        return len(self.indices)

    def __unicode__(self):
        # TODO: Better unicode/repr for GroupBy object
        return object.__repr__(self)

    @property
    def groups(self):
        """ dict {group name -> group labels} """
        return self.grouper.groups

    @property
    def ngroups(self):
        return self.grouper.ngroups

    @property
    def indices(self):
        """ dict {group name -> group indices} """
        return self.grouper.indices

    def _get_index(self, name):
        """ safe get index """
        try:
            return self.indices[name]
        except:
            if isinstance(name, Timestamp):
                name = name.value
                return self.indices[name]
            raise

    @property
    def name(self):
        if self._selection is None:
            return None  # 'result'
        else:
            return self._selection

    @property
    def _selection_list(self):
        if not isinstance(self._selection, (list, tuple, Series, np.ndarray)):
            return [self._selection]
        return self._selection

    def _local_dir(self):
        return sorted(set(self.obj._local_dir() + list(self._apply_whitelist)))

    def __getattr__(self, attr):
        if attr in self._internal_names_set:
            return object.__getattribute__(self, attr)
        if attr in self.obj:
            return self[attr]

        if hasattr(self.obj, attr):
            return self._make_wrapper(attr)

        raise AttributeError("%r object has no attribute %r" %
                             (type(self).__name__, attr))

    def __getitem__(self, key):
        raise NotImplementedError

    def _make_wrapper(self, name):
        if name not in self._apply_whitelist:
            is_callable = callable(getattr(self._selected_obj, name, None))
            kind = ' callable ' if is_callable else ' '
            msg = ("Cannot access{0}attribute {1!r} of {2!r} objects, try "
                   "using the 'apply' method".format(kind, name,
                                                     type(self).__name__))
            raise AttributeError(msg)

        f = getattr(self._selected_obj, name)
        if not isinstance(f, types.MethodType):
            return self.apply(lambda self: getattr(self, name))

        f = getattr(type(self._selected_obj), name)

        def wrapper(*args, **kwargs):
            # a little trickery for aggregation functions that need an axis
            # argument
            kwargs_with_axis = kwargs.copy()
            if 'axis' not in kwargs_with_axis:
                kwargs_with_axis['axis'] = self.axis

            def curried_with_axis(x):
                return f(x, *args, **kwargs_with_axis)

            def curried(x):
                return f(x, *args, **kwargs)

            # preserve the name so we can detect it when calling plot methods,
            # to avoid duplicates
            curried.__name__ = curried_with_axis.__name__ = name

            # special case otherwise extra plots are created when catching the
            # exception below
            if name in _plotting_methods:
                return self.apply(curried)

            try:
                return self.apply(curried_with_axis)
            except Exception:
                return self.apply(curried)

        return wrapper

    def get_group(self, name, obj=None):
        """
        Constructs NDFrame from group with provided name

        Parameters
        ----------
        name : object
            the name of the group to get as a DataFrame
        obj : NDFrame, default None
            the NDFrame to take the DataFrame out of.  If
            it is None, the object groupby was called on will
            be used

        Returns
        -------
        group : type of obj
        """
        if obj is None:
            obj = self._selected_obj

        inds = self._get_index(name)
        return obj.take(inds, axis=self.axis, convert=False)

    def __iter__(self):
        """
        Groupby iterator

        Returns
        -------
        Generator yielding sequence of (name, subsetted object)
        for each group
        """
        return self.grouper.get_iterator(self.obj, axis=self.axis)

    def apply(self, func, *args, **kwargs):
        """
        Apply function and combine results together in an intelligent way. The
        split-apply-combine combination rules attempt to be as common sense
        based as possible. For example:

        case 1:
        group DataFrame
        apply aggregation function (f(chunk) -> Series)
        yield DataFrame, with group axis having group labels

        case 2:
        group DataFrame
        apply transform function ((f(chunk) -> DataFrame with same indexes)
        yield DataFrame with resulting chunks glued together

        case 3:
        group Series
        apply function with f(chunk) -> DataFrame
        yield DataFrame with result of chunks glued together

        Parameters
        ----------
        func : function

        Notes
        -----
        See online documentation for full exposition on how to use apply

        See also
        --------
        aggregate, transform

        Returns
        -------
        applied : type depending on grouped object and function
        """
        func = _intercept_function(func)

        @wraps(func)
        def f(g):
            return func(g, *args, **kwargs)

        return self._python_apply_general(f)

    def _python_apply_general(self, f):
        keys, values, mutated = self.grouper.apply(f, self._selected_obj,
                                                   self.axis)

        return self._wrap_applied_output(keys, values,
                                         not_indexed_same=mutated)

    def aggregate(self, func, *args, **kwargs):
        raise NotImplementedError

    @Appender(_agg_doc)
    def agg(self, func, *args, **kwargs):
        return self.aggregate(func, *args, **kwargs)

    def _iterate_slices(self):
        yield self.name, self._selected_obj

    def transform(self, func, *args, **kwargs):
        raise NotImplementedError

    def mean(self):
        """
        Compute mean of groups, excluding missing values

        For multiple groupings, the result index will be a MultiIndex
        """
        try:
            return self._cython_agg_general('mean')
        except GroupByError:
            raise
        except Exception:  # pragma: no cover
            f = lambda x: x.mean(axis=self.axis)
            return self._python_agg_general(f)

    def median(self):
        """
        Compute median of groups, excluding missing values

        For multiple groupings, the result index will be a MultiIndex
        """
        try:
            return self._cython_agg_general('median')
        except GroupByError:
            raise
        except Exception:  # pragma: no cover

            def f(x):
                if isinstance(x, np.ndarray):
                    x = Series(x)
                return x.median(axis=self.axis)
            return self._python_agg_general(f)

    def std(self, ddof=1):
        """
        Compute standard deviation of groups, excluding missing values

        For multiple groupings, the result index will be a MultiIndex
        """
        # todo, implement at cython level?
        if ddof == 1:
            return self._cython_agg_general('std')
        else:
            f = lambda x: x.std(ddof=ddof)
            return self._python_agg_general(f)

    def var(self, ddof=1):
        """
        Compute variance of groups, excluding missing values

        For multiple groupings, the result index will be a MultiIndex
        """
        if ddof == 1:
            return self._cython_agg_general('var')
        else:
            f = lambda x: x.var(ddof=ddof)
            return self._python_agg_general(f)

    def size(self):
        """
        Compute group sizes
        """
        return self.grouper.size()

    sum = _groupby_function('sum', 'add', np.sum)
    prod = _groupby_function('prod', 'prod', np.prod)
    min = _groupby_function('min', 'min', np.min, numeric_only=False)
    max = _groupby_function('max', 'max', np.max, numeric_only=False)
    first = _groupby_function('first', 'first', _first_compat,
                              numeric_only=False, _convert=True)
    last = _groupby_function('last', 'last', _last_compat, numeric_only=False,
                             _convert=True)

    def ohlc(self):
        """
        Compute sum of values, excluding missing values

        For multiple groupings, the result index will be a MultiIndex

        """
        return self._cython_agg_general('ohlc')

    def nth(self, n, dropna=None):
        """
        Take the nth row from each group.

        If dropna, will not show nth non-null row, dropna is either
        Truthy (if a Series) or 'all', 'any' (if a DataFrame); this is equivalent
        to calling dropna(how=dropna) before the groupby.

        Examples
        --------
        >>> DataFrame([[1, np.nan], [1, 4], [5, 6]], columns=['A', 'B'])
        >>> g = df.groupby('A')
        >>> g.nth(0)
           A   B
        0  1 NaN
        2  5   6
        >>> g.nth(1)
           A  B
        1  1  4
        >>> g.nth(-1)
           A  B
        1  1  4
        2  5  6
        >>> g.nth(0, dropna='any')
           B
        A
        1  4
        5  6
        >>> g.nth(1, dropna='any')  # NaNs denote group exhausted when using dropna
            B
        A
        1 NaN
        5 NaN

        """

        if not dropna:  # good choice
            m = self.grouper._max_groupsize
            if n >= m or n < -m:
                return self._selected_obj.loc[[]]
            rng = np.zeros(m, dtype=bool)
            if n >= 0:
                rng[n] = True
                is_nth = self._cumcount_array(rng)
            else:
                rng[- n - 1] = True
                is_nth = self._cumcount_array(rng, ascending=False)
            return self._selected_obj[is_nth]

        if (isinstance(self._selected_obj, DataFrame)
           and dropna not in ['any', 'all']):
            # Note: when agg-ing picker doesn't raise this, just returns NaN
            raise ValueError("For a DataFrame groupby, dropna must be "
                             "either None, 'any' or 'all', "
                             "(was passed %s)." % (dropna),)

        # old behaviour, but with all and any support for DataFrames.

        max_len = n if n >= 0 else - 1 - n

        def picker(x):
            x = x.dropna(how=dropna)  # Note: how is ignored if Series
            if len(x) <= max_len:
                return np.nan
            else:
                return x.iloc[n]

        return self.agg(picker)

    def cumcount(self, **kwargs):
        """
        Number each item in each group from 0 to the length of that group - 1.

        Essentially this is equivalent to

        >>> self.apply(lambda x: Series(np.arange(len(x)), x.index))

        Parameters
        ----------
        ascending : bool, default True
            If False, number in reverse, from length of group - 1 to 0.

        Example
        -------

        >>> df = pd.DataFrame([['a'], ['a'], ['a'], ['b'], ['b'], ['a']],
        ...                   columns=['A'])
        >>> df
           A
        0  a
        1  a
        2  a
        3  b
        4  b
        5  a
        >>> df.groupby('A').cumcount()
        0    0
        1    1
        2    2
        3    0
        4    1
        5    3
        dtype: int64
        >>> df.groupby('A').cumcount(ascending=False)
        0    3
        1    2
        2    1
        3    1
        4    0
        5    0
        dtype: int64

        """
        ascending = kwargs.pop('ascending', True)

        index = self._selected_obj.index
        cumcounts = self._cumcount_array(ascending=ascending)
        return Series(cumcounts, index)

    def head(self, n=5):
        """
        Returns first n rows of each group.

        Essentially equivalent to ``.apply(lambda x: x.head(n))``,
        except ignores as_index flag.

        Example
        -------

        >>> df = DataFrame([[1, 2], [1, 4], [5, 6]],
                            columns=['A', 'B'])
        >>> df.groupby('A', as_index=False).head(1)
           A  B
        0  1  2
        2  5  6
        >>> df.groupby('A').head(1)
           A  B
        0  1  2
        2  5  6

        """
        obj = self._selected_obj
        in_head = self._cumcount_array() < n
        head = obj[in_head]
        return head

    def tail(self, n=5):
        """
        Returns last n rows of each group

        Essentially equivalent to ``.apply(lambda x: x.tail(n))``,
        except ignores as_index flag.

        Example
        -------

        >>> df = DataFrame([[1, 2], [1, 4], [5, 6]],
                            columns=['A', 'B'])
        >>> df.groupby('A', as_index=False).tail(1)
           A  B
        0  1  2
        2  5  6
        >>> df.groupby('A').head(1)
           A  B
        0  1  2
        2  5  6

        """
        obj = self._selected_obj
        rng = np.arange(0, -self.grouper._max_groupsize, -1, dtype='int64')
        in_tail = self._cumcount_array(rng, ascending=False) > -n
        tail = obj[in_tail]
        return tail

    def _cumcount_array(self, arr=None, **kwargs):
        """
        arr is where cumcount gets it's values from
        """
        ascending = kwargs.pop('ascending', True)

        if arr is None:
            arr = np.arange(self.grouper._max_groupsize, dtype='int64')

        len_index = len(self._selected_obj.index)
        cumcounts = np.empty(len_index, dtype=arr.dtype)

        if ascending:
            for v in self.indices.values():
                cumcounts[v] = arr[:len(v)]
        else:
            for v in self.indices.values():
                cumcounts[v] = arr[len(v)-1::-1]
        return cumcounts

    @cache_readonly
    def _selected_obj(self):
        if self._selection is None or isinstance(self.obj, Series):
            return self.obj
        else:
            return self.obj[self._selection]

    def _index_with_as_index(self, b):
        """
        Take boolean mask of index to be returned from apply, if as_index=True

        """
        # TODO perf, it feels like this should already be somewhere...
        from itertools import chain
        original = self._selected_obj.index
        gp = self.grouper
        levels = chain((gp.levels[i][gp.labels[i][b]]
                        for i in range(len(gp.groupings))),
                       (original.get_level_values(i)[b]
                        for i in range(original.nlevels)))
        new = MultiIndex.from_arrays(list(levels))
        new.names = gp.names + original.names
        return new

    def _try_cast(self, result, obj):
        """
        try to cast the result to our obj original type,
        we may have roundtripped thru object in the mean-time

        """
        if obj.ndim > 1:
            dtype = obj.values.dtype
        else:
            dtype = obj.dtype

        if not np.isscalar(result):
            result = _possibly_downcast_to_dtype(result, dtype)

        return result

    def _cython_agg_general(self, how, numeric_only=True):
        output = {}
        for name, obj in self._iterate_slices():
            is_numeric = is_numeric_dtype(obj.dtype)
            if numeric_only and not is_numeric:
                continue

            try:
                result, names = self.grouper.aggregate(obj.values, how)
            except AssertionError as e:
                raise GroupByError(str(e))
            output[name] = self._try_cast(result, obj)

        if len(output) == 0:
            raise DataError('No numeric types to aggregate')

        return self._wrap_aggregated_output(output, names)

    def _python_agg_general(self, func, *args, **kwargs):
        func = _intercept_function(func)
        f = lambda x: func(x, *args, **kwargs)

        # iterate through "columns" ex exclusions to populate output dict
        output = {}
        for name, obj in self._iterate_slices():
            try:
                result, counts = self.grouper.agg_series(obj, f)
                output[name] = self._try_cast(result, obj)
            except TypeError:
                continue

        if len(output) == 0:
            return self._python_apply_general(f)

        if self.grouper._filter_empty_groups:

            mask = counts.ravel() > 0
            for name, result in compat.iteritems(output):

                # since we are masking, make sure that we have a float object
                values = result
                if is_numeric_dtype(values.dtype):
                    values = com.ensure_float(values)

                output[name] = self._try_cast(values[mask], result)

        return self._wrap_aggregated_output(output)

    def _wrap_applied_output(self, *args, **kwargs):
        raise NotImplementedError

    def _concat_objects(self, keys, values, not_indexed_same=False):
        from pandas.tools.merge import concat

        if not not_indexed_same:
            result = concat(values, axis=self.axis)
            ax = self._selected_obj._get_axis(self.axis)

            if isinstance(result, Series):
                result = result.reindex(ax)
            else:
                result = result.reindex_axis(ax, axis=self.axis)
        elif self.group_keys and self.as_index:
            group_keys = keys
            group_levels = self.grouper.levels
            group_names = self.grouper.names
            result = concat(values, axis=self.axis, keys=group_keys,
                            levels=group_levels, names=group_names)
        else:
            result = concat(values, axis=self.axis)

        return result

    def _apply_filter(self, indices, dropna):
        if len(indices) == 0:
            indices = []
        else:
            indices = np.sort(np.concatenate(indices))
        if dropna:
            filtered = self._selected_obj.take(indices)
        else:
            mask = np.empty(len(self._selected_obj.index), dtype=bool)
            mask.fill(False)
            mask[indices.astype(int)] = True
            # mask fails to broadcast when passed to where; broadcast manually.
            mask = np.tile(mask, list(self._selected_obj.shape[1:]) + [1]).T
            filtered = self._selected_obj.where(mask)  # Fill with NaNs.
        return filtered


@Appender(GroupBy.__doc__)
def groupby(obj, by, **kwds):
    if isinstance(obj, Series):
        klass = SeriesGroupBy
    elif isinstance(obj, DataFrame):
        klass = DataFrameGroupBy
    else:  # pragma: no cover
        raise TypeError('invalid type: %s' % type(obj))

    return klass(obj, by, **kwds)


def _get_axes(group):
    if isinstance(group, Series):
        return [group.index]
    else:
        return group.axes


def _is_indexed_like(obj, axes):
    if isinstance(obj, Series):
        if len(axes) > 1:
            return False
        return obj.index.equals(axes[0])
    elif isinstance(obj, DataFrame):
        return obj.index.equals(axes[0])

    return False


class BaseGrouper(object):
    """
    This is an internal Grouper class, which actually holds the generated groups
    """

    def __init__(self, axis, groupings, sort=True, group_keys=True):
        self.axis = axis
        self.groupings = groupings
        self.sort = sort
        self.group_keys = group_keys
        self.compressed = True

    @property
    def shape(self):
        return tuple(ping.ngroups for ping in self.groupings)

    def __iter__(self):
        return iter(self.indices)

    @property
    def nkeys(self):
        return len(self.groupings)

    def get_iterator(self, data, axis=0):
        """
        Groupby iterator

        Returns
        -------
        Generator yielding sequence of (name, subsetted object)
        for each group
        """
        splitter = self._get_splitter(data, axis=axis)
        keys = self._get_group_keys()
        for key, (i, group) in zip(keys, splitter):
            yield key, group

    def _get_splitter(self, data, axis=0):
        comp_ids, _, ngroups = self.group_info
        return get_splitter(data, comp_ids, ngroups, axis=axis)

    def _get_group_keys(self):
        if len(self.groupings) == 1:
            return self.levels[0]
        else:
            comp_ids, _, ngroups = self.group_info
            # provide "flattened" iterator for multi-group setting
            mapper = _KeyMapper(comp_ids, ngroups, self.labels, self.levels)
            return [mapper.get_key(i) for i in range(ngroups)]

    def apply(self, f, data, axis=0):
        mutated = False
        splitter = self._get_splitter(data, axis=axis)
        group_keys = self._get_group_keys()

        # oh boy
        if (f.__name__ not in _plotting_methods and
                hasattr(splitter, 'fast_apply') and axis == 0):
            try:
                values, mutated = splitter.fast_apply(f, group_keys)
                return group_keys, values, mutated
            except (lib.InvalidApply):
                # we detect a mutation of some kind
                # so take slow path
                pass
            except (Exception) as e:
                # raise this error to the caller
                pass

        result_values = []
        for key, (i, group) in zip(group_keys, splitter):
            object.__setattr__(group, 'name', key)

            # group might be modified
            group_axes = _get_axes(group)
            res = f(group)
            if not _is_indexed_like(res, group_axes):
                mutated = True
            result_values.append(res)

        return group_keys, result_values, mutated

    @cache_readonly
    def indices(self):
        """ dict {group name -> group indices} """
        if len(self.groupings) == 1:
            return self.groupings[0].indices
        else:
            label_list = [ping.labels for ping in self.groupings]
            keys = [ping.group_index for ping in self.groupings]
            return _get_indices_dict(label_list, keys)

    @property
    def labels(self):
        return [ping.labels for ping in self.groupings]

    @property
    def levels(self):
        return [ping.group_index for ping in self.groupings]

    @property
    def names(self):
        return [ping.name for ping in self.groupings]

    def size(self):
        """
        Compute group sizes

        """
        # TODO: better impl
        labels, _, ngroups = self.group_info
        bin_counts = algos.value_counts(labels, sort=False)
        bin_counts = bin_counts.reindex(np.arange(ngroups))
        bin_counts.index = self.result_index
        return bin_counts

    @cache_readonly
    def _max_groupsize(self):
        '''
        Compute size of largest group

        '''
        # For many items in each group this is much faster than
        # self.size().max(), in worst case marginally slower
        if self.indices:
            return max(len(v) for v in self.indices.values())
        else:
            return 0

    @cache_readonly
    def groups(self):
        """ dict {group name -> group labels} """
        if len(self.groupings) == 1:
            return self.groupings[0].groups
        else:
            to_groupby = lzip(*(ping.grouper for ping in self.groupings))
            to_groupby = Index(to_groupby)

            return self.axis.groupby(to_groupby)

    @cache_readonly
    def group_info(self):
        comp_ids, obs_group_ids = self._get_compressed_labels()

        ngroups = len(obs_group_ids)
        comp_ids = com._ensure_int64(comp_ids)
        return comp_ids, obs_group_ids, ngroups

    def _get_compressed_labels(self):
        all_labels = [ping.labels for ping in self.groupings]
        if self._overflow_possible:
            tups = lib.fast_zip(all_labels)
            labs, uniques = algos.factorize(tups)

            if self.sort:
                uniques, labs = _reorder_by_uniques(uniques, labs)

            return labs, uniques
        else:
            if len(all_labels) > 1:
                group_index = get_group_index(all_labels, self.shape)
                comp_ids, obs_group_ids = _compress_group_index(group_index)
            else:
                ping = self.groupings[0]
                comp_ids = ping.labels
                obs_group_ids = np.arange(len(ping.group_index))
                self.compressed = False
                self._filter_empty_groups = False

            return comp_ids, obs_group_ids

    @cache_readonly
    def _overflow_possible(self):
        return _int64_overflow_possible(self.shape)

    @cache_readonly
    def ngroups(self):
        return len(self.result_index)

    @cache_readonly
    def result_index(self):
        recons = self.get_group_levels()
        return MultiIndex.from_arrays(recons, names=self.names)

    def get_group_levels(self):
        obs_ids = self.group_info[1]

        if not self.compressed and len(self.groupings) == 1:
            return [self.groupings[0].group_index]

        if self._overflow_possible:
            recons_labels = [np.array(x) for x in zip(*obs_ids)]
        else:
            recons_labels = decons_group_index(obs_ids, self.shape)

        name_list = []
        for ping, labels in zip(self.groupings, recons_labels):
            labels = com._ensure_platform_int(labels)
            name_list.append(ping.group_index.take(labels))

        return name_list

    #------------------------------------------------------------
    # Aggregation functions

    _cython_functions = {
        'add': 'group_add',
        'prod': 'group_prod',
        'min': 'group_min',
        'max': 'group_max',
        'mean': 'group_mean',
        'median': {
            'name': 'group_median'
        },
        'var': 'group_var',
        'std': 'group_var',
        'first': {
            'name': 'group_nth',
            'f': lambda func, a, b, c, d: func(a, b, c, d, 1)
        },
        'last': 'group_last',
    }

    _cython_transforms = {
        'std': np.sqrt
    }

    _cython_arity = {
        'ohlc': 4,  # OHLC
    }

    _name_functions = {}

    _filter_empty_groups = True

    def _get_aggregate_function(self, how, values):

        dtype_str = values.dtype.name

        def get_func(fname):
            # find the function, or use the object function, or return a
            # generic
            for dt in [dtype_str, 'object']:
                f = getattr(_algos, "%s_%s" % (fname, dtype_str), None)
                if f is not None:
                    return f
            return getattr(_algos, fname, None)

        ftype = self._cython_functions[how]

        if isinstance(ftype, dict):
            func = afunc = get_func(ftype['name'])

            # a sub-function
            f = ftype.get('f')
            if f is not None:

                def wrapper(*args, **kwargs):
                    return f(afunc, *args, **kwargs)

                # need to curry our sub-function
                func = wrapper

        else:
            func = get_func(ftype)

        if func is None:
            raise NotImplementedError("function is not implemented for this"
                                      "dtype: [how->%s,dtype->%s]" %
                                      (how, dtype_str))
        return func, dtype_str

    def aggregate(self, values, how, axis=0):

        arity = self._cython_arity.get(how, 1)

        vdim = values.ndim
        swapped = False
        if vdim == 1:
            values = values[:, None]
            out_shape = (self.ngroups, arity)
        else:
            if axis > 0:
                swapped = True
                values = values.swapaxes(0, axis)
            if arity > 1:
                raise NotImplementedError
            out_shape = (self.ngroups,) + values.shape[1:]

        if is_numeric_dtype(values.dtype):
            values = com.ensure_float(values)
            is_numeric = True
        else:
            if issubclass(values.dtype.type, np.datetime64):
                raise Exception('Cython not able to handle this case')

            values = values.astype(object)
            is_numeric = False

        # will be filled in Cython function
        result = np.empty(out_shape, dtype=values.dtype)
        result.fill(np.nan)
        counts = np.zeros(self.ngroups, dtype=np.int64)

        result = self._aggregate(result, counts, values, how, is_numeric)

        if self._filter_empty_groups:
            if result.ndim == 2:
                if is_numeric:
                    result = lib.row_bool_subset(
                        result, (counts > 0).view(np.uint8))
                else:
                    result = lib.row_bool_subset_object(
                        result, (counts > 0).view(np.uint8))
            else:
                result = result[counts > 0]

        if vdim == 1 and arity == 1:
            result = result[:, 0]

        if how in self._name_functions:
            # TODO
            names = self._name_functions[how]()
        else:
            names = None

        if swapped:
            result = result.swapaxes(0, axis)

        return result, names

    def _aggregate(self, result, counts, values, how, is_numeric):
        agg_func, dtype = self._get_aggregate_function(how, values)
        trans_func = self._cython_transforms.get(how, lambda x: x)

        comp_ids, _, ngroups = self.group_info
        if values.ndim > 3:
            # punting for now
            raise NotImplementedError
        elif values.ndim > 2:
            for i, chunk in enumerate(values.transpose(2, 0, 1)):

                chunk = chunk.squeeze()
                agg_func(result[:, :, i], counts, chunk, comp_ids)
        else:
            agg_func(result, counts, values, comp_ids)

        return trans_func(result)

    def agg_series(self, obj, func):
        try:
            return self._aggregate_series_fast(obj, func)
        except Exception:
            return self._aggregate_series_pure_python(obj, func)

    def _aggregate_series_fast(self, obj, func):
        func = _intercept_function(func)

        if obj.index._has_complex_internals:
            raise TypeError('Incompatible index for Cython grouper')

        group_index, _, ngroups = self.group_info

        # avoids object / Series creation overhead
        dummy = obj._get_values(slice(None, 0)).to_dense()
        indexer = _algos.groupsort_indexer(group_index, ngroups)[0]
        obj = obj.take(indexer, convert=False)
        group_index = com.take_nd(group_index, indexer, allow_fill=False)
        grouper = lib.SeriesGrouper(obj, func, group_index, ngroups,
                                    dummy)
        result, counts = grouper.get_result()
        return result, counts

    def _aggregate_series_pure_python(self, obj, func):

        group_index, _, ngroups = self.group_info

        counts = np.zeros(ngroups, dtype=int)
        result = None

        splitter = get_splitter(obj, group_index, ngroups, axis=self.axis)

        for label, group in splitter:
            res = func(group)
            if result is None:
                if (isinstance(res, (Series, np.ndarray)) or
                        isinstance(res, list)):
                    raise ValueError('Function does not reduce')
                result = np.empty(ngroups, dtype='O')

            counts[label] = group.shape[0]
            result[label] = res

        result = lib.maybe_convert_objects(result, try_float=0)
        return result, counts


def generate_bins_generic(values, binner, closed):
    """
    Generate bin edge offsets and bin labels for one array using another array
    which has bin edge values. Both arrays must be sorted.

    Parameters
    ----------
    values : array of values
    binner : a comparable array of values representing bins into which to bin
        the first array. Note, 'values' end-points must fall within 'binner'
        end-points.
    closed : which end of bin is closed; left (default), right

    Returns
    -------
    bins : array of offsets (into 'values' argument) of bins.
        Zero and last edge are excluded in result, so for instance the first
        bin is values[0:bin[0]] and the last is values[bin[-1]:]
    """
    lenidx = len(values)
    lenbin = len(binner)

    if lenidx <= 0 or lenbin <= 0:
        raise ValueError("Invalid length for values or for binner")

    # check binner fits data
    if values[0] < binner[0]:
        raise ValueError("Values falls before first bin")

    if values[lenidx - 1] > binner[lenbin - 1]:
        raise ValueError("Values falls after last bin")

    bins = np.empty(lenbin - 1, dtype=np.int64)

    j = 0  # index into values
    bc = 0  # bin count

    # linear scan, presume nothing about values/binner except that it fits ok
    for i in range(0, lenbin - 1):
        r_bin = binner[i + 1]

        # count values in current bin, advance to next bin
        while j < lenidx and (values[j] < r_bin or
                              (closed == 'right' and values[j] == r_bin)):
            j += 1

        bins[bc] = j
        bc += 1

    return bins

class BinGrouper(BaseGrouper):

    def __init__(self, bins, binlabels, filter_empty=False):
        self.bins = com._ensure_int64(bins)
        self.binlabels = _ensure_index(binlabels)
        self._filter_empty_groups = filter_empty

    @cache_readonly
    def groups(self):
        """ dict {group name -> group labels} """

        # this is mainly for compat
        # GH 3881
        return dict(zip(self.binlabels,self.bins))

    @property
    def nkeys(self):
        return 1

    def get_iterator(self, data, axis=0):
        """
        Groupby iterator

        Returns
        -------
        Generator yielding sequence of (name, subsetted object)
        for each group
        """
        if isinstance(data, NDFrame):
            slicer = lambda start,edge: data._slice(slice(start,edge),axis=axis)
            length = len(data.axes[axis])
        else:
            slicer = lambda start,edge: data[slice(start,edge)]
            length = len(data)

        start = 0
        for edge, label in zip(self.bins, self.binlabels):
            yield label, slicer(start,edge)
            start = edge

        if start < length:
            yield self.binlabels[-1], slicer(start,None)

    def apply(self, f, data, axis=0):
        result_keys = []
        result_values = []
        mutated = False
        for key, group in self.get_iterator(data, axis=axis):
            object.__setattr__(group, 'name', key)

            # group might be modified
            group_axes = _get_axes(group)
            res = f(group)

            if not _is_indexed_like(res, group_axes):
                mutated = True

            result_keys.append(key)
            result_values.append(res)

        return result_keys, result_values, mutated

    @cache_readonly
    def ngroups(self):
        return len(self.binlabels)

    @cache_readonly
    def result_index(self):
        return self.binlabels

    @property
    def levels(self):
        return [self.binlabels]

    @property
    def names(self):
        return [self.binlabels.name]

    #----------------------------------------------------------------------
    # cython aggregation

    _cython_functions = {
        'add': 'group_add_bin',
        'prod': 'group_prod_bin',
        'mean': 'group_mean_bin',
        'min': 'group_min_bin',
        'max': 'group_max_bin',
        'var': 'group_var_bin',
        'std': 'group_var_bin',
        'ohlc': 'group_ohlc',
        'first': {
            'name': 'group_nth_bin',
            'f': lambda func, a, b, c, d: func(a, b, c, d, 1)
        },
        'last': 'group_last_bin',
    }

    _name_functions = {
        'ohlc': lambda *args: ['open', 'high', 'low', 'close']
    }

    _filter_empty_groups = True

    def _aggregate(self, result, counts, values, how, is_numeric=True):

        agg_func, dtype = self._get_aggregate_function(how, values)
        trans_func = self._cython_transforms.get(how, lambda x: x)

        if values.ndim > 3:
            # punting for now
            raise NotImplementedError
        elif values.ndim > 2:
            for i, chunk in enumerate(values.transpose(2, 0, 1)):
                agg_func(result[:, :, i], counts, chunk, self.bins)
        else:
            agg_func(result, counts, values, self.bins)

        return trans_func(result)

    def agg_series(self, obj, func):
        dummy = obj[:0]
        grouper = lib.SeriesBinGrouper(obj, func, self.bins, dummy)
        return grouper.get_result()


class Grouping(object):

    """
    Holds the grouping information for a single key

    Parameters
    ----------
    index : Index
    grouper :
    obj :
    name :
    level :

    Returns
    -------
    **Attributes**:
      * indices : dict of {group -> index_list}
      * labels : ndarray, group labels
      * ids : mapping of label -> group
      * counts : array of group counts
      * group_index : unique groups
      * groups : dict of {group -> label_list}
    """

    def __init__(self, index, grouper=None, obj=None, name=None, level=None,
                 sort=True):

        self.name = name
        self.level = level
        self.grouper = _convert_grouper(index, grouper)
        self.index = index
        self.sort = sort
        self.obj = obj

        # right place for this?
        if isinstance(grouper, (Series, Index)) and name is None:
            self.name = grouper.name

        if isinstance(grouper, MultiIndex):
            self.grouper = grouper.values

        # pre-computed
        self._was_factor = False
        self._should_compress = True

        # we have a single grouper which may be a myriad of things, some of which are
        # dependent on the passing in level
        #

        if level is not None:
            if not isinstance(level, int):
                if level not in index.names:
                    raise AssertionError('Level %s not in index' % str(level))
                level = index.names.index(level)

            inds = index.labels[level]
            level_index = index.levels[level]

            if self.name is None:
                self.name = index.names[level]

            # XXX complete hack

            if grouper is not None:
                level_values = index.levels[level].take(inds)
                self.grouper = level_values.map(self.grouper)
            else:
                self._was_factor = True

                # all levels may not be observed
                labels, uniques = algos.factorize(inds, sort=True)

                if len(uniques) > 0 and uniques[0] == -1:
                    # handle NAs
                    mask = inds != -1
                    ok_labels, uniques = algos.factorize(inds[mask], sort=True)

                    labels = np.empty(len(inds), dtype=inds.dtype)
                    labels[mask] = ok_labels
                    labels[-mask] = -1

                if len(uniques) < len(level_index):
                    level_index = level_index.take(uniques)

                self._labels = labels
                self._group_index = level_index
                self.grouper = level_index.take(labels)
        else:
            if isinstance(self.grouper, (list, tuple)):
                self.grouper = com._asarray_tuplesafe(self.grouper)

            # a passed Categorical
            elif isinstance(self.grouper, Categorical):

                factor = self.grouper
                self._was_factor = True

                # Is there any way to avoid this?
                self.grouper = np.asarray(factor)

                self._labels = factor.labels
                self._group_index = factor.levels
                if self.name is None:
                    self.name = factor.name

            # a passed Grouper like
            elif isinstance(self.grouper, Grouper):

                # get the new grouper
                grouper = self.grouper._get_binner_for_grouping(self.obj)
                self.obj = self.grouper.obj
                self.grouper = grouper
                if self.name is None:
                    self.name = grouper.name

            # no level passed
            if not isinstance(self.grouper, (Series, np.ndarray)):
                self.grouper = self.index.map(self.grouper)
                if not (hasattr(self.grouper, "__len__") and
                        len(self.grouper) == len(self.index)):
                    errmsg = ('Grouper result violates len(labels) == '
                              'len(data)\nresult: %s' %
                              com.pprint_thing(self.grouper))
                    self.grouper = None  # Try for sanity
                    raise AssertionError(errmsg)

        # if we have a date/time-like grouper, make sure that we have Timestamps like
        if getattr(self.grouper,'dtype',None) is not None:
            if is_datetime64_dtype(self.grouper):
                from pandas import to_datetime
                self.grouper = to_datetime(self.grouper)
            elif is_timedelta64_dtype(self.grouper):
                from pandas import to_timedelta
                self.grouper = to_timedelta(self.grouper)

    def __repr__(self):
        return 'Grouping(%s)' % self.name

    def __iter__(self):
        return iter(self.indices)

    _labels = None
    _group_index = None

    @property
    def ngroups(self):
        return len(self.group_index)

    @cache_readonly
    def indices(self):
        return _groupby_indices(self.grouper)

    @property
    def labels(self):
        if self._labels is None:
            self._make_labels()
        return self._labels

    @property
    def group_index(self):
        if self._group_index is None:
            self._make_labels()
        return self._group_index

    def _make_labels(self):
        if self._was_factor:  # pragma: no cover
            raise Exception('Should not call this method grouping by level')
        else:
            labs, uniques = algos.factorize(self.grouper, sort=self.sort)
            uniques = Index(uniques, name=self.name)
            self._labels = labs
            self._group_index = uniques

    _groups = None

    @property
    def groups(self):
        if self._groups is None:
            self._groups = self.index.groupby(self.grouper)
        return self._groups


def _get_grouper(obj, key=None, axis=0, level=None, sort=True):
    """
    create and return a BaseGrouper, which is an internal
    mapping of how to create the grouper indexers.
    This may be composed of multiple Grouping objects, indicating
    multiple groupers

    Groupers are ultimately index mappings. They can originate as:
    index mappings, keys to columns, functions, or Groupers

    Groupers enable local references to axis,level,sort, while
    the passed in axis, level, and sort are 'global'.

    This routine tries to figure of what the passing in references
    are and then creates a Grouping for each one, combined into
    a BaseGrouper.

    """

    group_axis = obj._get_axis(axis)

    # validate thatthe passed level is compatible with the passed
    # axis of the object
    if level is not None:
        if not isinstance(group_axis, MultiIndex):
            if isinstance(level, compat.string_types):
                if obj.index.name != level:
                    raise ValueError('level name %s is not the name of the '
                                     'index' % level)
            elif level > 0:
                raise ValueError('level > 0 only valid with MultiIndex')

            level = None
            key = group_axis

    # a passed in Grouper, directly convert
    if isinstance(key, Grouper):
        binner, grouper, obj = key._get_grouper(obj)
        return grouper, [], obj

    # already have a BaseGrouper, just return it
    elif isinstance(key, BaseGrouper):
        return key, [], obj

    if not isinstance(key, (tuple, list)):
        keys = [key]
    else:
        keys = key

    # what are we after, exactly?
    match_axis_length = len(keys) == len(group_axis)
    any_callable = any(callable(g) or isinstance(g, dict) for g in keys)
    any_arraylike = any(isinstance(g, (list, tuple, Series, np.ndarray))
                        for g in keys)

    try:
        if isinstance(obj, DataFrame):
            all_in_columns = all(g in obj.columns for g in keys)
        else:
            all_in_columns = False
    except Exception:
        all_in_columns = False

    if (not any_callable and not all_in_columns
        and not any_arraylike and match_axis_length
            and level is None):
        keys = [com._asarray_tuplesafe(keys)]

    if isinstance(level, (tuple, list)):
        if key is None:
            keys = [None] * len(level)
        levels = level
    else:
        levels = [level] * len(keys)

    groupings = []
    exclusions = []
    for i, (gpr, level) in enumerate(zip(keys, levels)):
        name = None
        try:
            obj._data.items.get_loc(gpr)
            in_axis = True
        except Exception:
            in_axis = False

        if _is_label_like(gpr) or in_axis:
            exclusions.append(gpr)
            name = gpr
            gpr = obj[gpr]

        if isinstance(gpr, Categorical) and len(gpr) != len(obj):
            errmsg = "Categorical grouper must have len(grouper) == len(data)"
            raise AssertionError(errmsg)

        ping = Grouping(group_axis, gpr, obj=obj, name=name, level=level, sort=sort)
        groupings.append(ping)

    if len(groupings) == 0:
        raise ValueError('No group keys passed!')

    # create the internals grouper
    grouper = BaseGrouper(group_axis, groupings, sort=sort)

    return grouper, exclusions, obj


def _is_label_like(val):
    return isinstance(val, compat.string_types) or np.isscalar(val)


def _convert_grouper(axis, grouper):
    if isinstance(grouper, dict):
        return grouper.get
    elif isinstance(grouper, Series):
        if grouper.index.equals(axis):
            return grouper.values
        else:
            return grouper.reindex(axis).values
    elif isinstance(grouper, (list, Series, np.ndarray)):
        if len(grouper) != len(axis):
            raise AssertionError('Grouper and axis must be same length')
        return grouper
    else:
        return grouper


class SeriesGroupBy(GroupBy):
    _apply_whitelist = _series_apply_whitelist

    def aggregate(self, func_or_funcs, *args, **kwargs):
        """
        Apply aggregation function or functions to groups, yielding most likely
        Series but in some cases DataFrame depending on the output of the
        aggregation function

        Parameters
        ----------
        func_or_funcs : function or list / dict of functions
            List/dict of functions will produce DataFrame with column names
            determined by the function names themselves (list) or the keys in
            the dict

        Notes
        -----
        agg is an alias for aggregate. Use it.

        Examples
        --------
        >>> series
        bar    1.0
        baz    2.0
        qot    3.0
        qux    4.0

        >>> mapper = lambda x: x[0] # first letter
        >>> grouped = series.groupby(mapper)

        >>> grouped.aggregate(np.sum)
        b    3.0
        q    7.0

        >>> grouped.aggregate([np.sum, np.mean, np.std])
           mean  std  sum
        b  1.5   0.5  3
        q  3.5   0.5  7

        >>> grouped.agg({'result' : lambda x: x.mean() / x.std(),
        ...              'total' : np.sum})
           result  total
        b  2.121   3
        q  4.95    7

        See also
        --------
        apply, transform

        Returns
        -------
        Series or DataFrame
        """
        if isinstance(func_or_funcs, compat.string_types):
            return getattr(self, func_or_funcs)(*args, **kwargs)

        if hasattr(func_or_funcs, '__iter__'):
            ret = self._aggregate_multiple_funcs(func_or_funcs)
        else:
            cyfunc = _intercept_cython(func_or_funcs)
            if cyfunc and not args and not kwargs:
                return getattr(self, cyfunc)()

            if self.grouper.nkeys > 1:
                return self._python_agg_general(func_or_funcs, *args, **kwargs)

            try:
                return self._python_agg_general(func_or_funcs, *args, **kwargs)
            except Exception:
                result = self._aggregate_named(func_or_funcs, *args, **kwargs)

            index = Index(sorted(result), name=self.grouper.names[0])
            ret = Series(result, index=index)

        if not self.as_index:  # pragma: no cover
            print('Warning, ignoring as_index=True')

        return ret

    def _aggregate_multiple_funcs(self, arg):
        if isinstance(arg, dict):
            columns = list(arg.keys())
            arg = list(arg.items())
        elif any(isinstance(x, (tuple, list)) for x in arg):
            arg = [(x, x) if not isinstance(x, (tuple, list)) else x
                   for x in arg]

            # indicated column order
            columns = lzip(*arg)[0]
        else:
            # list of functions / function names
            columns = []
            for f in arg:
                if isinstance(f, compat.string_types):
                    columns.append(f)
                else:
                    columns.append(f.__name__)
            arg = lzip(columns, arg)

        results = {}

        for name, func in arg:
            if name in results:
                raise SpecificationError('Function names must be unique, '
                                         'found multiple named %s' % name)

            results[name] = self.aggregate(func)

        return DataFrame(results, columns=columns)

    def _wrap_aggregated_output(self, output, names=None):
        # sort of a kludge
        output = output[self.name]
        index = self.grouper.result_index

        if names is not None:
            return DataFrame(output, index=index, columns=names)
        else:
            return Series(output, index=index, name=self.name)

    def _wrap_applied_output(self, keys, values, not_indexed_same=False):
        if len(keys) == 0:
            # GH #6265
            return Series([], name=self.name)

        def _get_index():
            if self.grouper.nkeys > 1:
                index = MultiIndex.from_tuples(keys, names=self.grouper.names)
            else:
                index = Index(keys, name=self.grouper.names[0])
            return index

        if isinstance(values[0], dict):
            # GH #823
            index = _get_index()
            return DataFrame(values, index=index).stack()

        if isinstance(values[0], (Series, dict)):
            return self._concat_objects(keys, values,
                                        not_indexed_same=not_indexed_same)
        elif isinstance(values[0], DataFrame):
            # possible that Series -> DataFrame by applied function
            return self._concat_objects(keys, values,
                                        not_indexed_same=not_indexed_same)
        else:
            # GH #6265
            return Series(values, index=_get_index(), name=self.name)

    def _aggregate_named(self, func, *args, **kwargs):
        result = {}

        for name, group in self:
            group.name = name
            output = func(group, *args, **kwargs)
            if isinstance(output, (Series, np.ndarray)):
                raise Exception('Must produce aggregated value')
            result[name] = self._try_cast(output, group)

        return result

    def transform(self, func, *args, **kwargs):
        """
        Call function producing a like-indexed Series on each group and return
        a Series with the transformed values

        Parameters
        ----------
        func : function
            To apply to each group. Should return a Series with the same index

        Examples
        --------
        >>> grouped.transform(lambda x: (x - x.mean()) / x.std())

        Returns
        -------
        transformed : Series
        """
        result = self._selected_obj.copy()
        if hasattr(result, 'values'):
            result = result.values
        dtype = result.dtype

        if isinstance(func, compat.string_types):
            wrapper = lambda x: getattr(x, func)(*args, **kwargs)
        else:
            wrapper = lambda x: func(x, *args, **kwargs)

        for name, group in self:

            object.__setattr__(group, 'name', name)
            res = wrapper(group)
            if hasattr(res, 'values'):
                res = res.values

            # need to do a safe put here, as the dtype may be different
            # this needs to be an ndarray
            result = Series(result)
            result.iloc[self._get_index(name)] = res
            result = result.values

        # downcast if we can (and need)
        result = _possibly_downcast_to_dtype(result, dtype)
        return self._selected_obj.__class__(result, index=self._selected_obj.index,
                                  name=self._selected_obj.name)

    def filter(self, func, dropna=True, *args, **kwargs):
        """
        Return a copy of a Series excluding elements from groups that
        do not satisfy the boolean criterion specified by func.

        Parameters
        ----------
        func : function
            To apply to each group. Should return True or False.
        dropna : Drop groups that do not pass the filter. True by default;
            if False, groups that evaluate False are filled with NaNs.

        Example
        -------
        >>> grouped.filter(lambda x: x.mean() > 0)

        Returns
        -------
        filtered : Series
        """
        if isinstance(func, compat.string_types):
            wrapper = lambda x: getattr(x, func)(*args, **kwargs)
        else:
            wrapper = lambda x: func(x, *args, **kwargs)

        # Interpret np.nan as False.
        def true_and_notnull(x, *args, **kwargs):
            b = wrapper(x, *args, **kwargs)
            return b and notnull(b)

        try:
            indices = [self._get_index(name) if true_and_notnull(group) else []
                       for name, group in self]
        except ValueError:
            raise TypeError("the filter must return a boolean result")
        except TypeError:
            raise TypeError("the filter must return a boolean result")

        filtered = self._apply_filter(indices, dropna)
        return filtered


class NDFrameGroupBy(GroupBy):

    def _iterate_slices(self):
        if self.axis == 0:
            # kludge
            if self._selection is None:
                slice_axis = self.obj.columns
            else:
                slice_axis = self._selection_list
            slicer = lambda x: self.obj[x]
        else:
            slice_axis = self.obj.index
            slicer = self.obj.xs

        for val in slice_axis:
            if val in self.exclusions:
                continue
            yield val, slicer(val)

    def _cython_agg_general(self, how, numeric_only=True):
        new_blocks = self._cython_agg_blocks(how, numeric_only=numeric_only)
        return self._wrap_agged_blocks(new_blocks)

    def _wrap_agged_blocks(self, blocks):
        obj = self._obj_with_exclusions

        new_axes = list(obj._data.axes)

        # more kludge
        if self.axis == 0:
            new_axes[0], new_axes[1] = new_axes[1], self.grouper.result_index
        else:
            new_axes[self.axis] = self.grouper.result_index

        mgr = BlockManager(blocks, new_axes)

        new_obj = type(obj)(mgr)

        return self._post_process_cython_aggregate(new_obj)

    _block_agg_axis = 0

    def _cython_agg_blocks(self, how, numeric_only=True):
        data, agg_axis = self._get_data_to_aggregate()

        new_blocks = []

        for block in data.blocks:
            values = block.values

            is_numeric = is_numeric_dtype(values.dtype)

            if numeric_only and not is_numeric:
                continue

            if is_numeric:
                values = com.ensure_float(values)

            result, _ = self.grouper.aggregate(values, how, axis=agg_axis)

            # see if we can cast the block back to the original dtype
            result = block._try_cast_result(result)

            newb = make_block(result, block.items, block.ref_items)
            new_blocks.append(newb)

        if len(new_blocks) == 0:
            raise DataError('No numeric types to aggregate')

        return new_blocks

    def _get_data_to_aggregate(self):
        obj = self._obj_with_exclusions
        if self.axis == 0:
            return obj.swapaxes(0, 1)._data, 1
        else:
            return obj._data, self.axis

    def _post_process_cython_aggregate(self, obj):
        # undoing kludge from below
        if self.axis == 0:
            obj = obj.swapaxes(0, 1)
        return obj

    @cache_readonly
    def _obj_with_exclusions(self):
        if self._selection is not None:
            return self.obj.reindex(columns=self._selection_list)

        if len(self.exclusions) > 0:
            return self.obj.drop(self.exclusions, axis=1)
        else:
            return self.obj

    @Appender(_agg_doc)
    def aggregate(self, arg, *args, **kwargs):
        if isinstance(arg, compat.string_types):
            return getattr(self, arg)(*args, **kwargs)

        result = OrderedDict()
        if isinstance(arg, dict):
            if self.axis != 0:  # pragma: no cover
                raise ValueError('Can only pass dict with axis=0')

            obj = self._selected_obj

            if any(isinstance(x, (list, tuple, dict)) for x in arg.values()):
                new_arg = OrderedDict()
                for k, v in compat.iteritems(arg):
                    if not isinstance(v, (tuple, list, dict)):
                        new_arg[k] = [v]
                    else:
                        new_arg[k] = v
                arg = new_arg

            keys = []
            if self._selection is not None:
                subset = obj
                if isinstance(subset, DataFrame):
                    raise NotImplementedError

                for fname, agg_how in compat.iteritems(arg):
                    colg = SeriesGroupBy(subset, selection=self._selection,
                                         grouper=self.grouper)
                    result[fname] = colg.aggregate(agg_how)
                    keys.append(fname)
            else:
                for col, agg_how in compat.iteritems(arg):
                    colg = SeriesGroupBy(obj[col], selection=col,
                                         grouper=self.grouper)
                    result[col] = colg.aggregate(agg_how)
                    keys.append(col)

            if isinstance(list(result.values())[0], DataFrame):
                from pandas.tools.merge import concat
                result = concat([result[k] for k in keys], keys=keys, axis=1)
            else:
                result = DataFrame(result)
        elif isinstance(arg, list):
            return self._aggregate_multiple_funcs(arg)
        else:
            cyfunc = _intercept_cython(arg)
            if cyfunc and not args and not kwargs:
                return getattr(self, cyfunc)()

            if self.grouper.nkeys > 1:
                return self._python_agg_general(arg, *args, **kwargs)
            else:

                # try to treat as if we are passing a list
                try:
                    assert not args and not kwargs
                    result = self._aggregate_multiple_funcs([arg])
                    result.columns = Index(result.columns.levels[0],
                                           name=self._selected_obj.columns.name)
                except:
                    result = self._aggregate_generic(arg, *args, **kwargs)

        if not self.as_index:
            if isinstance(result.index, MultiIndex):
                zipped = zip(result.index.levels, result.index.labels,
                             result.index.names)
                for i, (lev, lab, name) in enumerate(zipped):
                    result.insert(i, name,
                                  com.take_nd(lev.values, lab,
                                              allow_fill=False))
                result = result.consolidate()
            else:
                values = result.index.values
                name = self.grouper.groupings[0].name
                result.insert(0, name, values)
            result.index = np.arange(len(result))

        return result.convert_objects()

    def _aggregate_multiple_funcs(self, arg):
        from pandas.tools.merge import concat

        if self.axis != 0:
            raise NotImplementedError

        obj = self._obj_with_exclusions

        results = []
        keys = []
        for col in obj:
            try:
                colg = SeriesGroupBy(obj[col], selection=col,
                                     grouper=self.grouper)
                results.append(colg.aggregate(arg))
                keys.append(col)
            except (TypeError, DataError):
                pass
            except SpecificationError:
                raise
        result = concat(results, keys=keys, axis=1)

        return result

    def _aggregate_generic(self, func, *args, **kwargs):
        if self.grouper.nkeys != 1:
            raise AssertionError('Number of keys must be 1')

        axis = self.axis
        obj = self._obj_with_exclusions

        result = {}
        if axis != obj._info_axis_number:
            try:
                for name, data in self:
                    # for name in self.indices:
                    #     data = self.get_group(name, obj=obj)
                    result[name] = self._try_cast(func(data, *args, **kwargs),
                                                  data)
            except Exception:
                return self._aggregate_item_by_item(func, *args, **kwargs)
        else:
            for name in self.indices:
                try:
                    data = self.get_group(name, obj=obj)
                    result[name] = self._try_cast(func(data, *args, **kwargs),
                                                  data)
                except Exception:
                    wrapper = lambda x: func(x, *args, **kwargs)
                    result[name] = data.apply(wrapper, axis=axis)

        return self._wrap_generic_output(result, obj)

    def _wrap_aggregated_output(self, output, names=None):
        raise NotImplementedError

    def _aggregate_item_by_item(self, func, *args, **kwargs):
        # only for axis==0

        obj = self._obj_with_exclusions
        result = {}
        cannot_agg = []
        errors=None
        for item in obj:
            try:
                data = obj[item]
                colg = SeriesGroupBy(data, selection=item,
                                     grouper=self.grouper)
                result[item] = self._try_cast(
                    colg.aggregate(func, *args, **kwargs), data)
            except ValueError:
                cannot_agg.append(item)
                continue
            except TypeError as e:
                cannot_agg.append(item)
                errors=e
                continue

        result_columns = obj.columns
        if cannot_agg:
            result_columns = result_columns.drop(cannot_agg)

            # GH6337
            if not len(result_columns) and errors is not None:
                raise errors

        return DataFrame(result, columns=result_columns)

    def _decide_output_index(self, output, labels):
        if len(output) == len(labels):
            output_keys = labels
        else:
            output_keys = sorted(output)
            try:
                output_keys.sort()
            except Exception:  # pragma: no cover
                pass

            if isinstance(labels, MultiIndex):
                output_keys = MultiIndex.from_tuples(output_keys,
                                                     names=labels.names)

        return output_keys

    def _wrap_applied_output(self, keys, values, not_indexed_same=False):
        from pandas.core.index import _all_indexes_same

        if len(keys) == 0:
            # XXX
            return DataFrame({})

        key_names = self.grouper.names

        if isinstance(values[0], DataFrame):
            return self._concat_objects(keys, values,
                                        not_indexed_same=not_indexed_same)
        elif hasattr(self.grouper, 'groupings'):
            if len(self.grouper.groupings) > 1:
                key_index = MultiIndex.from_tuples(keys, names=key_names)
            else:
                ping = self.grouper.groupings[0]
                if len(keys) == ping.ngroups:
                    key_index = ping.group_index
                    key_index.name = key_names[0]

                    key_lookup = Index(keys)
                    indexer = key_lookup.get_indexer(key_index)

                    # reorder the values
                    values = [values[i] for i in indexer]
                else:
                    key_index = Index(keys, name=key_names[0])

            # make Nones an empty object
            if com._count_not_none(*values) != len(values):
                v = next(v for v in values if v is not None)
                if v is None:
                    return DataFrame()
                elif isinstance(v, NDFrame):
                    values = [
                        x if x is not None else
                        v._constructor(**v._construct_axes_dict())
                        for x in values
                        ]

            v = values[0]

            if isinstance(v, (np.ndarray, Series)):
                if isinstance(v, Series):
                    applied_index = self._selected_obj._get_axis(self.axis)
                    all_indexed_same = _all_indexes_same([
                        x.index for x in values
                    ])
                    singular_series = (len(values) == 1 and
                                       applied_index.nlevels == 1)

                    # GH3596
                    # provide a reduction (Frame -> Series) if groups are
                    # unique
                    if self.squeeze:

                        # assign the name to this series
                        if singular_series:
                            values[0].name = keys[0]

                            # GH2893
                            # we have series in the values array, we want to
                            # produce a series:
                            # if any of the sub-series are not indexed the same
                            # OR we don't have a multi-index and we have only a
                            # single values
                            return self._concat_objects(
                                keys, values, not_indexed_same=not_indexed_same
                            )

                        # still a series
                        # path added as of GH 5545
                        elif all_indexed_same:
                            from pandas.tools.merge import concat
                            return concat(values)

                    if not all_indexed_same:
                        return self._concat_objects(
                            keys, values, not_indexed_same=not_indexed_same
                        )

                try:
                    if self.axis == 0:
                        # GH6124 if the list of Series have a consistent name,
                        # then propagate that name to the result.
                        index = v.index.copy()
                        if index.name is None:
                            # Only propagate the series name to the result
                            # if all series have a consistent name.  If the
                            # series do not have a consistent name, do
                            # nothing.
                            names = set(v.name for v in values)
                            if len(names) == 1:
                                index.name = list(names)[0]

                        # normally use vstack as its faster than concat
                        # and if we have mi-columns
                        if not _np_version_under1p7 or isinstance(v.index,MultiIndex):
                            stacked_values = np.vstack([np.asarray(x) for x in values])
                            result = DataFrame(stacked_values,index=key_index,columns=index)
                        else:
                            # GH5788 instead of stacking; concat gets the dtypes correct
                            from pandas.tools.merge import concat
                            result = concat(values,keys=key_index,names=key_index.names,
                                            axis=self.axis).unstack()
                            result.columns = index
                    else:
                        stacked_values = np.vstack([np.asarray(x) for x in values])
                        result = DataFrame(stacked_values.T,index=v.index,columns=key_index)

                except (ValueError, AttributeError):
                    # GH1738: values is list of arrays of unequal lengths fall
                    # through to the outer else caluse
                    return Series(values, index=key_index)

                # if we have date/time like in the original, then coerce dates
                # as we are stacking can easily have object dtypes here
                if (self._selected_obj.ndim == 2
                       and self._selected_obj.dtypes.isin(_DATELIKE_DTYPES).any()):
                    cd = 'coerce'
                else:
                    cd = True
                return result.convert_objects(convert_dates=cd)

            else:
                # only coerce dates if we find at least 1 datetime
                cd = 'coerce' if any([ isinstance(v,Timestamp) for v in values ]) else False
                return Series(values, index=key_index).convert_objects(convert_dates=cd)

        else:
            # Handle cases like BinGrouper
            return self._concat_objects(keys, values,
                                        not_indexed_same=not_indexed_same)

    def transform(self, func, *args, **kwargs):
        """
        Call function producing a like-indexed DataFrame on each group and
        return a DataFrame having the same indexes as the original object
        filled with the transformed values

        Parameters
        ----------
        f : function
            Function to apply to each subframe

        Notes
        -----
        Each subframe is endowed the attribute 'name' in case you need to know
        which group you are working on.

        Examples
        --------
        >>> grouped = df.groupby(lambda x: mapping[x])
        >>> grouped.transform(lambda x: (x - x.mean()) / x.std())
        """
        from pandas.tools.merge import concat

        applied = []

        obj = self._obj_with_exclusions
        gen = self.grouper.get_iterator(obj, axis=self.axis)
        fast_path, slow_path = self._define_paths(func, *args, **kwargs)

        path = None
        for name, group in gen:
            object.__setattr__(group, 'name', name)

            if path is None:
                # Try slow path and fast path.
                try:
                    path, res = self._choose_path(fast_path, slow_path, group)
                except TypeError:
                    return self._transform_item_by_item(obj, fast_path)
                except Exception:  # pragma: no cover
                    res = fast_path(group)
                    path = fast_path
            else:
                res = path(group)

            # broadcasting
            if isinstance(res, Series):
                if res.index.is_(obj.index):
                    group.T.values[:] = res
                else:
                    group.values[:] = res

                applied.append(group)
            else:
                applied.append(res)

        concat_index = obj.columns if self.axis == 0 else obj.index
        concatenated = concat(applied, join_axes=[concat_index],
                              axis=self.axis, verify_integrity=False)
        concatenated.sort_index(inplace=True)
        return concatenated

    def _define_paths(self, func, *args, **kwargs):
        if isinstance(func, compat.string_types):
            fast_path = lambda group: getattr(group, func)(*args, **kwargs)
            slow_path = lambda group: group.apply(
                lambda x: getattr(x, func)(*args, **kwargs), axis=self.axis)
        else:
            fast_path = lambda group: func(group, *args, **kwargs)
            slow_path = lambda group: group.apply(
                lambda x: func(x, *args, **kwargs), axis=self.axis)
        return fast_path, slow_path

    def _choose_path(self, fast_path, slow_path, group):
        path = slow_path
        res = slow_path(group)

        # if we make it here, test if we can use the fast path
        try:
            res_fast = fast_path(group)

            # compare that we get the same results
            if res.shape == res_fast.shape:
                res_r = res.values.ravel()
                res_fast_r = res_fast.values.ravel()
                mask = notnull(res_r)
            if (res_r[mask] == res_fast_r[mask]).all():
                path = fast_path

        except:
            pass
        return path, res

    def _transform_item_by_item(self, obj, wrapper):
        # iterate through columns
        output = {}
        inds = []
        for i, col in enumerate(obj):
            try:
                output[col] = self[col].transform(wrapper)
                inds.append(i)
            except Exception:
                pass

        if len(output) == 0:  # pragma: no cover
            raise TypeError('Transform function invalid for data types')

        columns = obj.columns
        if len(output) < len(obj.columns):
            columns = columns.take(inds)

        return DataFrame(output, index=obj.index, columns=columns)

    def filter(self, func, dropna=True, *args, **kwargs):
        """
        Return a copy of a DataFrame excluding elements from groups that
        do not satisfy the boolean criterion specified by func.

        Parameters
        ----------
        f : function
            Function to apply to each subframe. Should return True or False.
        dropna : Drop groups that do not pass the filter. True by default;
            if False, groups that evaluate False are filled with NaNs.

        Notes
        -----
        Each subframe is endowed the attribute 'name' in case you need to know
        which group you are working on.

        Example
        --------
        >>> grouped = df.groupby(lambda x: mapping[x])
        >>> grouped.filter(lambda x: x['A'].sum() + x['B'].sum() > 0)
        """
        from pandas.tools.merge import concat

        indices = []

        obj = self._selected_obj
        gen = self.grouper.get_iterator(obj, axis=self.axis)

        fast_path, slow_path = self._define_paths(func, *args, **kwargs)

        path = None
        for name, group in gen:
            object.__setattr__(group, 'name', name)

            if path is None:
                # Try slow path and fast path.
                try:
                    path, res = self._choose_path(fast_path, slow_path, group)
                except Exception:  # pragma: no cover
                    res = fast_path(group)
                    path = fast_path
            else:
                res = path(group)

            def add_indices():
                indices.append(self._get_index(name))

            # interpret the result of the filter
            if isinstance(res, (bool, np.bool_)):
                if res:
                    add_indices()
            else:
                if getattr(res, 'ndim', None) == 1:
                    val = res.ravel()[0]
                    if val and notnull(val):
                        add_indices()
                else:

                    # in theory you could do .all() on the boolean result ?
                    raise TypeError("the filter must return a boolean result")

        filtered = self._apply_filter(indices, dropna)
        return filtered


class DataFrameGroupBy(NDFrameGroupBy):
    _apply_whitelist = _dataframe_apply_whitelist

    _block_agg_axis = 1

    def __getitem__(self, key):
        if self._selection is not None:
            raise Exception('Column(s) %s already selected' % self._selection)

        if (isinstance(key, (list, tuple, Series, np.ndarray)) or
                not self.as_index):
            return DataFrameGroupBy(self.obj, self.grouper, selection=key,
                                    grouper=self.grouper,
                                    exclusions=self.exclusions,
                                    as_index=self.as_index)
        else:
            if key not in self.obj:  # pragma: no cover
                raise KeyError(str(key))
            # kind of a kludge
            return SeriesGroupBy(self.obj[key], selection=key,
                                 grouper=self.grouper,
                                 exclusions=self.exclusions)

    def _wrap_generic_output(self, result, obj):
        result_index = self.grouper.levels[0]

        if result:
            if self.axis == 0:
                result = DataFrame(result, index=obj.columns,
                                   columns=result_index).T
            else:
                result = DataFrame(result, index=obj.index,
                                   columns=result_index)
        else:
            result = DataFrame(result)

        return result

    def _get_data_to_aggregate(self):
        obj = self._obj_with_exclusions
        if self.axis == 1:
            return obj.T._data, 1
        else:
            return obj._data, 1

    def _wrap_aggregated_output(self, output, names=None):
        agg_axis = 0 if self.axis == 1 else 1
        agg_labels = self._obj_with_exclusions._get_axis(agg_axis)

        output_keys = self._decide_output_index(output, agg_labels)

        if not self.as_index:
            result = DataFrame(output, columns=output_keys)
            group_levels = self.grouper.get_group_levels()
            zipped = zip(self.grouper.names, group_levels)

            for i, (name, labels) in enumerate(zipped):
                result.insert(i, name, labels)
            result = result.consolidate()
        else:
            index = self.grouper.result_index
            result = DataFrame(output, index=index, columns=output_keys)

        if self.axis == 1:
            result = result.T

        return result.convert_objects()

    def _wrap_agged_blocks(self, blocks):
        obj = self._obj_with_exclusions

        if self.axis == 0:
            agg_labels = obj.columns
        else:
            agg_labels = obj.index

        if sum(len(x.items) for x in blocks) == len(agg_labels):
            output_keys = agg_labels
        else:
            all_items = []
            for b in blocks:
                all_items.extend(b.items)
            output_keys = agg_labels[agg_labels.isin(all_items)]

            for blk in blocks:
                blk.set_ref_items(output_keys, maybe_rename=False)

        if not self.as_index:
            index = np.arange(blocks[0].values.shape[1])
            mgr = BlockManager(blocks, [output_keys, index])
            result = DataFrame(mgr)

            group_levels = self.grouper.get_group_levels()
            zipped = zip(self.grouper.names, group_levels)

            for i, (name, labels) in enumerate(zipped):
                result.insert(i, name, labels)
            result = result.consolidate()
        else:
            index = self.grouper.result_index
            mgr = BlockManager(blocks, [output_keys, index])
            result = DataFrame(mgr)

        if self.axis == 1:
            result = result.T

        return result.convert_objects()

    def _iterate_column_groupbys(self):
        for i, colname in enumerate(self._selected_obj.columns):
            yield colname, SeriesGroupBy(self._selected_obj.iloc[:, i],
                                         selection=colname,
                                         grouper=self.grouper,
                                         exclusions=self.exclusions)

    def _apply_to_column_groupbys(self, func):
        from pandas.tools.merge import concat
        return concat(
            (func(col_groupby) for _, col_groupby
             in self._iterate_column_groupbys()),
            keys=self._selected_obj.columns, axis=1)

    def ohlc(self):
        """
        Compute sum of values, excluding missing values

        For multiple groupings, the result index will be a MultiIndex
        """
        return self._apply_to_column_groupbys(
            lambda x: x._cython_agg_general('ohlc'))


from pandas.tools.plotting import boxplot_frame_groupby
DataFrameGroupBy.boxplot = boxplot_frame_groupby


class PanelGroupBy(NDFrameGroupBy):

    def _iterate_slices(self):
        if self.axis == 0:
            # kludge
            if self._selection is None:
                slice_axis = self._selected_obj.items
            else:
                slice_axis = self._selection_list
            slicer = lambda x: self._selected_obj[x]
        else:
            raise NotImplementedError

        for val in slice_axis:
            if val in self.exclusions:
                continue

            yield val, slicer(val)

    def aggregate(self, arg, *args, **kwargs):
        """
        Aggregate using input function or dict of {column -> function}

        Parameters
        ----------
        arg : function or dict
            Function to use for aggregating groups. If a function, must either
            work when passed a Panel or when passed to Panel.apply. If
            pass a dict, the keys must be DataFrame column names

        Returns
        -------
        aggregated : Panel
        """
        if isinstance(arg, compat.string_types):
            return getattr(self, arg)(*args, **kwargs)

        return self._aggregate_generic(arg, *args, **kwargs)

    def _wrap_generic_output(self, result, obj):
        if self.axis == 0:
            new_axes = list(obj.axes)
            new_axes[0] = self.grouper.result_index
        elif self.axis == 1:
            x, y, z = obj.axes
            new_axes = [self.grouper.result_index, z, x]
        else:
            x, y, z = obj.axes
            new_axes = [self.grouper.result_index, y, x]

        result = Panel._from_axes(result, new_axes)

        if self.axis == 1:
            result = result.swapaxes(0, 1).swapaxes(0, 2)
        elif self.axis == 2:
            result = result.swapaxes(0, 2)

        return result

    def _aggregate_item_by_item(self, func, *args, **kwargs):
        obj = self._obj_with_exclusions
        result = {}

        if self.axis > 0:
            for item in obj:
                try:
                    itemg = DataFrameGroupBy(obj[item],
                                             axis=self.axis - 1,
                                             grouper=self.grouper)
                    result[item] = itemg.aggregate(func, *args, **kwargs)
                except (ValueError, TypeError):
                    raise
            new_axes = list(obj.axes)
            new_axes[self.axis] = self.grouper.result_index
            return Panel._from_axes(result, new_axes)
        else:
            raise NotImplementedError

    def _wrap_aggregated_output(self, output, names=None):
        raise NotImplementedError


class NDArrayGroupBy(GroupBy):
    pass


#----------------------------------------------------------------------
# Splitting / application


class DataSplitter(object):

    def __init__(self, data, labels, ngroups, axis=0):
        self.data = data
        self.labels = com._ensure_int64(labels)
        self.ngroups = ngroups

        self.axis = axis

    @cache_readonly
    def slabels(self):
        # Sorted labels
        return com.take_nd(self.labels, self.sort_idx, allow_fill=False)

    @cache_readonly
    def sort_idx(self):
        # Counting sort indexer
        return _algos.groupsort_indexer(self.labels, self.ngroups)[0]

    def __iter__(self):
        sdata = self._get_sorted_data()

        if self.ngroups == 0:
            raise StopIteration

        starts, ends = lib.generate_slices(self.slabels, self.ngroups)

        for i, (start, end) in enumerate(zip(starts, ends)):
            # Since I'm now compressing the group ids, it's now not "possible"
            # to produce empty slices because such groups would not be observed
            # in the data
            # if start >= end:
            #     raise AssertionError('Start %s must be less than end %s'
            #                          % (str(start), str(end)))
            yield i, self._chop(sdata, slice(start, end))

    def _get_sorted_data(self):
        return self.data.take(self.sort_idx, axis=self.axis, convert=False)

    def _chop(self, sdata, slice_obj):
        return sdata.iloc[slice_obj]

    def apply(self, f):
        raise NotImplementedError


class ArraySplitter(DataSplitter):
    pass


class SeriesSplitter(DataSplitter):

    def _chop(self, sdata, slice_obj):
        return sdata._get_values(slice_obj).to_dense()


class FrameSplitter(DataSplitter):

    def __init__(self, data, labels, ngroups, axis=0):
        super(FrameSplitter, self).__init__(data, labels, ngroups, axis=axis)

    def fast_apply(self, f, names):
        # must return keys::list, values::list, mutated::bool
        try:
            starts, ends = lib.generate_slices(self.slabels, self.ngroups)
        except:
            # fails when all -1
            return [], True

        sdata = self._get_sorted_data()
        results, mutated = lib.apply_frame_axis0(sdata, f, names, starts, ends)

        return results, mutated

    def _chop(self, sdata, slice_obj):
        if self.axis == 0:
            return sdata.iloc[slice_obj]
        else:
            return sdata._slice(slice_obj, axis=1)  # ix[:, slice_obj]


class NDFrameSplitter(DataSplitter):

    def __init__(self, data, labels, ngroups, axis=0):
        super(NDFrameSplitter, self).__init__(data, labels, ngroups, axis=axis)

        self.factory = data._constructor

    def _get_sorted_data(self):
        # this is the BlockManager
        data = self.data._data

        # this is sort of wasteful but...
        sorted_axis = data.axes[self.axis].take(self.sort_idx)
        sorted_data = data.reindex_axis(sorted_axis, axis=self.axis)

        return sorted_data

    def _chop(self, sdata, slice_obj):
        return self.factory(sdata.get_slice(slice_obj, axis=self.axis))


def get_splitter(data, *args, **kwargs):
    if isinstance(data, Series):
        klass = SeriesSplitter
    elif isinstance(data, DataFrame):
        klass = FrameSplitter
    else:
        klass = NDFrameSplitter

    return klass(data, *args, **kwargs)


#----------------------------------------------------------------------
# Misc utilities


def get_group_index(label_list, shape):
    """
    For the particular label_list, gets the offsets into the hypothetical list
    representing the totally ordered cartesian product of all possible label
    combinations.
    """
    if len(label_list) == 1:
        return label_list[0]

    n = len(label_list[0])
    group_index = np.zeros(n, dtype=np.int64)
    mask = np.zeros(n, dtype=bool)
    for i in range(len(shape)):
        stride = np.prod([x for x in shape[i + 1:]], dtype=np.int64)
        group_index += com._ensure_int64(label_list[i]) * stride
        mask |= label_list[i] < 0

    np.putmask(group_index, mask, -1)
    return group_index

_INT64_MAX = np.iinfo(np.int64).max


def _int64_overflow_possible(shape):
    the_prod = long(1)
    for x in shape:
        the_prod *= long(x)

    return the_prod >= _INT64_MAX


def decons_group_index(comp_labels, shape):
    # reconstruct labels
    label_list = []
    factor = 1
    y = 0
    x = comp_labels
    for i in reversed(range(len(shape))):
        labels = (x - y) % (factor * shape[i]) // factor
        np.putmask(labels, comp_labels < 0, -1)
        label_list.append(labels)
        y = labels * factor
        factor *= shape[i]
    return label_list[::-1]


def _indexer_from_factorized(labels, shape, compress=True):
    if _int64_overflow_possible(shape):
        indexer = np.lexsort(np.array(labels[::-1]))
        return indexer

    group_index = get_group_index(labels, shape)

    if compress:
        comp_ids, obs_ids = _compress_group_index(group_index)
        max_group = len(obs_ids)
    else:
        comp_ids = group_index
        max_group = com._long_prod(shape)

    if max_group > 1e6:
        # Use mergesort to avoid memory errors in counting sort
        indexer = comp_ids.argsort(kind='mergesort')
    else:
        indexer, _ = _algos.groupsort_indexer(comp_ids.astype(np.int64),
                                              max_group)

    return indexer


def _lexsort_indexer(keys, orders=None, na_position='last'):
    labels = []
    shape = []
    if isinstance(orders, bool):
        orders = [orders] * len(keys)
    elif orders is None:
        orders = [True] * len(keys)

    for key, order in zip(keys, orders):
        key = np.asanyarray(key)
        rizer = _hash.Factorizer(len(key))

        if not key.dtype == np.object_:
            key = key.astype('O')

        # factorize maps nans to na_sentinel=-1
        ids = rizer.factorize(key, sort=True)
        n = len(rizer.uniques)
        mask = (ids == -1)
        if order: # ascending
            if na_position == 'last':
                ids = np.where(mask, n, ids)
            elif na_position == 'first':
                ids += 1
            else:
                raise ValueError('invalid na_position: {!r}'.format(na_position))
        else: # not order means descending
            if na_position == 'last':
                ids = np.where(mask, n, n-ids-1)
            elif na_position == 'first':
                ids = np.where(mask, 0, n-ids)
            else:
                raise ValueError('invalid na_position: {!r}'.format(na_position))
        if mask.any():
            n += 1
        shape.append(n)
        labels.append(ids)
    return _indexer_from_factorized(labels, shape)

def _nargsort(items, kind='quicksort', ascending=True, na_position='last'):
    """
    This is intended to be a drop-in replacement for np.argsort which handles NaNs
    It adds ascending and na_position parameters.
    GH #6399, #5231
    """
    items = np.asanyarray(items)
    idx = np.arange(len(items))
    mask = isnull(items)
    non_nans = items[~mask]
    non_nan_idx = idx[~mask]
    nan_idx = np.nonzero(mask)[0]
    if not ascending:
        non_nans = non_nans[::-1]
        non_nan_idx = non_nan_idx[::-1]
    indexer = non_nan_idx[non_nans.argsort(kind=kind)]
    if not ascending:
        indexer = indexer[::-1]
    # Finally, place the NaNs at the end or the beginning according to na_position
    if na_position == 'last':
        indexer = np.concatenate([indexer, nan_idx])
    elif na_position == 'first':
        indexer = np.concatenate([nan_idx, indexer])
    else:
        raise ValueError('invalid na_position: {!r}'.format(na_position))
    return indexer


class _KeyMapper(object):

    """
    Ease my suffering. Map compressed group id -> key tuple
    """

    def __init__(self, comp_ids, ngroups, labels, levels):
        self.levels = levels
        self.labels = labels
        self.comp_ids = comp_ids.astype(np.int64)

        self.k = len(labels)
        self.tables = [_hash.Int64HashTable(ngroups) for _ in range(self.k)]

        self._populate_tables()

    def _populate_tables(self):
        for labs, table in zip(self.labels, self.tables):
            table.map(self.comp_ids, labs.astype(np.int64))

    def get_key(self, comp_id):
        return tuple(level[table.get_item(comp_id)]
                     for table, level in zip(self.tables, self.levels))


def _get_indices_dict(label_list, keys):
    shape = [len(x) for x in keys]
    group_index = get_group_index(label_list, shape)

    sorter, _ = _algos.groupsort_indexer(com._ensure_int64(group_index),
                                         np.prod(shape))

    sorter_int = com._ensure_platform_int(sorter)

    sorted_labels = [lab.take(sorter_int) for lab in label_list]
    group_index = group_index.take(sorter_int)

    return lib.indices_fast(sorter, group_index, keys, sorted_labels)


#----------------------------------------------------------------------
# sorting levels...cleverly?


def _compress_group_index(group_index, sort=True):
    """
    Group_index is offsets into cartesian product of all possible labels. This
    space can be huge, so this function compresses it, by computing offsets
    (comp_ids) into the list of unique labels (obs_group_ids).
    """

    table = _hash.Int64HashTable(min(1000000, len(group_index)))

    group_index = com._ensure_int64(group_index)

    # note, group labels come out ascending (ie, 1,2,3 etc)
    comp_ids, obs_group_ids = table.get_labels_groupby(group_index)

    if sort and len(obs_group_ids) > 0:
        obs_group_ids, comp_ids = _reorder_by_uniques(obs_group_ids, comp_ids)

    return comp_ids, obs_group_ids


def _reorder_by_uniques(uniques, labels):
    # sorter is index where elements ought to go
    sorter = uniques.argsort()

    # reverse_indexer is where elements came from
    reverse_indexer = np.empty(len(sorter), dtype=np.int64)
    reverse_indexer.put(sorter, np.arange(len(sorter)))

    mask = labels < 0

    # move labels to right locations (ie, unsort ascending labels)
    labels = com.take_nd(reverse_indexer, labels, allow_fill=False)
    np.putmask(labels, mask, -1)

    # sort observed ids
    uniques = com.take_nd(uniques, sorter, allow_fill=False)

    return uniques, labels


_func_table = {
    builtins.sum: np.sum
}


_cython_table = {
    builtins.sum: 'sum',
    np.sum: 'sum',
    np.mean: 'mean',
    np.prod: 'prod',
    np.std: 'std',
    np.var: 'var',
    np.median: 'median',
    np.max: 'max',
    np.min: 'min'
}


def _intercept_function(func):
    return _func_table.get(func, func)


def _intercept_cython(func):
    return _cython_table.get(func)


def _groupby_indices(values):
    return _algos.groupby_indices(com._ensure_object(values))


def numpy_groupby(data, labels, axis=0):
    s = np.argsort(labels)
    keys, inv = np.unique(labels, return_inverse=True)
    i = inv.take(s)
    groups_at = np.where(i != np.concatenate(([-1], i[:-1])))[0]
    ordered_data = data.take(s, axis=axis)
    group_sums = np.add.reduceat(ordered_data, groups_at, axis=axis)

    return group_sums
