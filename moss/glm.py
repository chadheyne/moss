from __future__ import division

try:
    from StringIO import StringIO
    import cPickle
except:  # PY3
    from io import StringIO
    import pickle as cPickle
import numpy as np
import scipy as sp
import pandas as pd
from scipy.stats import gamma
from sklearn.decomposition import PCA
import matplotlib.pyplot as plt

import seaborn as sns


class HRFModel(object):
    """Abstract class definition for HRF Models."""
    def __init__(self):
        raise NotImplementedError

    @property
    def kernel(self):
        """Evaluate the kernal at timepoints."""
        raise NotImplementedError

    def convolve(self, data):
        """Convolve the kernel with some data."""
        raise NotImplementedError


class IdentityHRF(object):
    """HRF that does not transform the data. Mostly useful for testing."""
    def convolve(self, data, frametimes, name):
        return pd.DataFrame({name: data}, index=frametimes)


class GammaDifferenceHRF(HRFModel):
    """Canonical difference of gamma variates HRF model."""
    def __init__(self, temporal_deriv=False, tr=2, oversampling=16,
                 kernel_secs=32, pos_shape=6, pos_scale=1,
                 neg_shape=16, neg_scale=1, ratio=1./6):
        """Create the HRF object with FSL parameters as default."""
        self._rv_pos = gamma(pos_shape, scale=pos_scale)
        self._rv_neg = gamma(neg_shape, scale=neg_scale)
        self._tr = tr
        self._oversampling = oversampling
        dt = tr / oversampling
        self._timepoints = np.linspace(0, kernel_secs, kernel_secs / dt)
        self._temporal_deriv = temporal_deriv
        self._ratio = ratio

    @property
    def kernel(self):
        """Evaluate the kernel at timepoints, maybe with derivative."""
        y = self._rv_pos.pdf(self._timepoints)
        y -= self._ratio * self._rv_neg.pdf(self._timepoints)
        y /= y.sum()

        if self._temporal_deriv:
            dy = np.diff(y)
            dy = np.concatenate([[0], dy])
            scale = np.sqrt(np.square(y).sum() / np.square(dy).sum())
            dy *= scale
            y = np.c_[y, dy]
        else:
            y = np.c_[y]

        return y

    def convolve(self, data, frametimes=None, name=None):
        """Convolve the kernel with some data.

        Parameters
        ----------
        data : Series or 1d array
            data to convolve
        frametimes : Series or 1d array, optional
            timepoints corresponding to data - if None, assume
            data is sampled with same TR and oversampling as kernal
        name : string
            name to associate with data if not passing Series object

        Returns
        -------
        out : DataFrame
            n_cols depends on whether kernel has temporal derivative

        """
        ntp = len(data)

        # Without frametimes, assume data and kernel have same sampling
        if frametimes is None:
            orig_ntp = ntp / self._oversampling
            frametimes = np.arange(0, orig_ntp * self._tr,
                                   self._tr / self._oversampling)

        # Get the output name for this condition
        if name is None:
            try:
                name = data.name
            except AttributeError:
                name = "event"
        cols = [name]

        # Obtain the current kernel and set up the output
        kernel = self.kernel.T
        out = np.empty((ntp, len(kernel)))

        # Do the convolution
        if self._temporal_deriv:
            cols.append(name + "_deriv")
            main, deriv = kernel
            out[:, 0] = np.convolve(data, main)[:ntp]
            out[:, 1] = np.convolve(data, deriv)[:ntp]
        else:
            out[:, 0] = np.convolve(data, kernel.ravel())[:ntp]

        # Build the output DataFrame
        out = pd.DataFrame(out, columns=cols, index=frametimes)
        return out


class FIR(HRFModel):
    """Finite Impule Response HRF model."""
    def __init__(self):

        return NotImplementedError


class DesignMatrix(object):
    """fMRI-specific design matrix object.

    This class creates a design matrix that is most directly consistent with
    FSL; i.e., the design is filtered with the gaussian running-line approach
    implemented by fslmaths, and the columns are de-meaned (so there is no
    constant regressor in the final model).

    Note that only the regressors resulting from the passed design are
    high-pass filtered. If your other regressors of interest, or confound
    regressors must be filtered, it is up to you to filter them on your own.

    The matrix is represented by a Pandas DataFrame with the ev names in the
    columns and the frametimes in the index. In addition to the full design
    matrix, this object also exposes views on some subsets of the data:

    - main_submatrix
        the condition evs that are created from the experimental design
        along with any continuous regressors or interest. if the HRF
        model includes a temporal derivative, those columns are not
        included in this submatrix
    - condition_submatrix
        only the condition ev columns
    - confound_submatrix
        the continuous evs considered representing nuisance variables
    - artifact_submatrix
        the set of indicator vectors used to censor individual frames
        from the model

    In the case where one of these components does not exist in the
    matrix (e.g., no frames had artifacts), these attributes are `None`.

    """
    def __init__(self, design, hrf_model, ntp, regressors=None, confounds=None,
                 artifacts=None, condition_names=None, confound_pca=False,
                 tr=2, hpf_cutoff=128, oversampling=16):
        """Initialize the design matrix object.

        Parameters
        ----------
        design : DataFrame
            at a minimum, this dataframe must have `condition` and `onset`
            columns. the `duration` and `value` (aka amplitude) of each element
            can also be specified, with a default duration of 0 (i.e. an
            impulse) and value of 1. onset and duration are specified in
            seconds. if a value is not fiven for `condition names`, the
            resulting design is formed from the sorted unique value in the
            condition column.
        hrf_model : HRFModel class
            this class must specify its own "convolution" semantics
        ntp : int
            number of timepoints in the data
        regressors : array or DataFrame
            other regressors of interest (e.g. timecourse of data from some
            seed ROI). if a DataFrame, index must have the frametimes and
            match those inferred from the `ntp` and `tr` arguments. the
            columns are de-meaned, but not filtered or otherwise transformed.
        confounds : array or DataFrame
            similar to `regressors`, but considered to be of no interest
            (e.g., motion parameters).
        artifacts : boolean(esque) array with length equal to `ntp`
            a mask indicating frames that have some kind of artifact. this
            information is transformed into a set of indicator vectors.
        condition_names : list of string
            a subset of the names that can be found in the `condition`
            column of the design dataframe. can be used to exclude conditions
            from a particualr design or reorder the columns in the resulting
            matrix.
        confound_pca : bool
            if True, transform the confound matrix with PCA (using a maximum
            likelihood method to guess the dimensionality of the data)
        tr : float
            sampling interval (in seconds) of the data/design
        hpf_cutoff : float
            filter cutoff (in seconds), or None to skip the filter
        oversampling : float
            construction of the condition evs and convolution
            are performed on high-resolution data with this oversampling

        """
        if "duration" not in design:
            design["duration"] = 0
        if "value" not in design:
            design["value"] = 1

        self.design = design
        self.tr = tr

        stop = ntp * tr
        frametimes = np.arange(0, stop, tr, np.float)
        self.frametimes = pd.Series(frametimes, name="frametimes")

        stop = stop + 1 if oversampling == 1 else stop
        hires_frametimes = np.arange(0, stop, tr / oversampling, np.float)
        self._hires_frametimes = hires_frametimes

        if condition_names is None:
            condition_names = np.sort(design.condition.unique())
        self._condition_names = pd.Series(condition_names, name="conditions")

        self._ntp = ntp

        # Convolve the oversampled condition evs
        self._make_hires_base(oversampling)
        self._convolve(hrf_model)

        # Subsample the condition evs and highpass filter
        conditions = self._subsample_condition_matrix()
        conditions -= conditions.mean()
        pp_heights = (conditions.max() - conditions.min()).tolist()
        if hpf_cutoff is not None:
            conditions = self._highpass_filter(conditions, hpf_cutoff)

        # Set up the other regressors of interest
        regressors = self._validate_component(regressors, "regressor")

        # Set up the confound submatrix
        confounds = self._validate_component(confounds, "confound")

        if confound_pca:
            pca = PCA(0.99).fit_transform(confounds)
            n_conf = pca.shape[1]
            new_columns = pd.Series(["confound_%d"] * n_conf) % range(n_conf)
            confounds = pd.DataFrame(pca, confounds.index, new_columns)

        # Set up the artifacts submatrix
        if artifacts is not None:
            if artifacts.any():
                n_art = artifacts.sum()
                art = np.zeros((artifacts.size, n_art))
                art[np.where(artifacts.astype(bool)), np.arange(n_art)] = 1
                artifacts = self._validate_component(art, "artifact")
            else:
                artifacts = None

        # Now build the full design matrix
        pieces = [conditions]
        if regressors is not None:
            pieces.append(regressors)
        if confounds is not None:
            pieces.append(confounds)
        if artifacts is not None:
            pieces.append(artifacts)

        X = pd.concat(pieces, axis=1)
        X.index = self.frametimes
        X.columns.name = "evs"
        X -= X.mean(axis=0)
        self.design_matrix = X

        # Now build the column name lists that will let us index
        # into the submatrices
        conf_names, art_names = [], []
        main_names = self._condition_names.tolist()
        if regressors is not None:
            main_names += regressors.columns.tolist()
            pp_heights += (regressors.max() - regressors.min()).tolist()
        if confounds is not None:
            conf_names = confounds.columns.tolist()
            pp_heights += (confounds.max() - confounds.min()).tolist()
        if artifacts is not None:
            art_names = artifacts.columns.tolist()
            pp_heights += (artifacts.max() - artifacts.min()).tolist()
        self._full_names = X.columns.tolist()
        self._main_names = main_names
        self._confound_names = conf_names
        self._artifact_names = art_names

        # Set up boolean arrays that can be used to mask beta vectors
        cols = self.design_matrix.columns
        self.main_vector = cols.isin(main_names).reshape(-1, 1)
        self.condition_vector = cols.isin(self._condition_names).reshape(-1, 1)
        self.confound_vector, self.artifact_vector = None, None
        if confounds is not None:
            self.confound_vector = cols.isin(conf_names).reshape(-1, 1)
        if artifacts is not None:
            self.artifact_vector = cols.isin(art_names).reshape(-1, 1)

        # Here is the additional design information
        self._pp_heights = pp_heights
        self._singular_values = np.linalg.svd(self.design_matrix.values,
                                              compute_uv=False)

    def __repr__(self):
        """Represent the object with the design matrix."""
        return self.design_matrix.__repr__()

    def _repr_html_(self):
        """Represent the object with the design matrix."""
        return self.design_matrix._repr_html_()

    def _make_hires_base(self, oversampling):
        """Make the oversampled condition base submatrix."""
        hires_base = pd.DataFrame(columns=self._condition_names,
                                  index=self._hires_frametimes)

        for cond in self._condition_names:
            cond_info = self.design[self.design.condition == cond]
            cond_info = cond_info[["onset", "duration", "value"]]
            ev = self._make_hires_ev_base(cond_info)
            hires_base[cond] = ev
        self._hires_base = hires_base

    def _make_hires_ev_base(self, info):
        """Oversample a condition ev vector."""
        hft = self._hires_frametimes

        # Get the condition information
        onsets, durations, vals = info.values.T

        # Make the ev timecourse
        tmax = len(hft)
        ev = np.zeros_like(hft).astype(np.float)
        t_onset = np.minimum(np.searchsorted(hft, onsets), tmax - 1)
        ev[t_onset] += vals
        t_offset = np.minimum(np.searchsorted(hft, onsets + durations),
                              len(hft) - 1)

        # Handle the case where duration is 0 by offsetting at t + 1
        for i, off in enumerate(t_offset):
            if off < (tmax - 1) and off == t_onset[i]:
                t_offset[i] += 1

        ev[t_offset] -= vals
        ev = np.cumsum(ev)

        return ev

    def _convolve(self, hrf_model):
        """Convolve the condition evs with the HRF model."""
        self._hires_conditions = self._hires_base.copy()
        for cond in self._condition_names:
            res = hrf_model.convolve(self._hires_base[cond],
                                     self._hires_frametimes,
                                     cond)
            for key, vals in res.iteritems():
                self._hires_conditions[key] = vals

    def _subsample_condition_matrix(self):
        """Sample the hires convolved matrix at the TR midpoint."""
        condition_X = pd.DataFrame(columns=self._hires_conditions.columns,
                                   index=self.frametimes)

        frametime_midpoints = self.frametimes + self.tr / 2
        for key, vals in self._hires_conditions.iteritems():
            resampler = sp.interpolate.interp1d(self._hires_frametimes, vals,
                                                kind="nearest")
            condition_X[key] = resampler(frametime_midpoints)
        return condition_X

    def _validate_component(self, comp, name_base):
        """For components that can be an an array or df, build the df."""
        if comp is None:
            return None

        n = comp.shape[1]
        try:
            names = comp.columns
        except AttributeError:
            names = pd.Series([name_base + "_%d"] * n) % range(n)
            comp = pd.DataFrame(comp, self.frametimes, names)

        if names.tolist() == list(range(n)):
            names = pd.Series([name_base + "_%d"] * n) % range(n)
            comp.columns = names

        frametimes_match = (np.all(comp.index == self.frametimes) or
                            np.all(comp.index == np.arange(len(comp))))
        if not frametimes_match:
            err = "Frametimes for %ss do not match design." % name_base
            raise ValueError(err)

        comp.index = self.frametimes

        return comp

    def _highpass_filter(self, mat, cutoff):
        """Highpass-filter each column in mat."""
        F = fsl_highpass_matrix(self._ntp, cutoff, self.tr)
        for key, vals in mat.iteritems():
            mat[key] = np.dot(F, vals)
        return mat

    def contrast_vector(self, names, weights):
        """Return a full contrast vector given condition names and weights."""
        vector = np.zeros(self.design_matrix.shape[1])
        for name, weight in zip(names, weights):
            vector[self.design_matrix.columns == name] = weight
        return vector

    def plot(self, kind="full", fname=None, cmap="bone"):
        """Draw an image representation of the design matrix.

        Parameters
        ----------
        kind : string
            which submatrix to plot, or "full" for the whole matrix
        fname : string, optional
            if provided, save the plot to this file name
        cmap : string or colormap object
            colormap for the plot

        """
        names = getattr(self, "_%s_names" % kind)
        mat = self.design_matrix[names].copy()
        mat -= mat.min()
        mat /= mat.max()

        x, y = .66 * mat.shape[1], .04 * mat.shape[0]
        figsize = min(x, 10), min(y, 14)
        f, ax = plt.subplots(1, 1, figsize=figsize)
        ax.imshow(mat, aspect="auto", cmap=cmap, vmin=-.2, vmax=1.2,
                  interpolation="nearest", zorder=2)
        ax.set_yticks([])
        ax.set_xticks(range(len(names)))
        ax.set_xticklabels(names, ha="right", rotation=30)
        for x in range(len(names) - 1):
            ax.axvline(x + .5, c="k", lw=3, zorder=3)
        plt.tight_layout()

        if fname is not None:
            f.savefig(fname)

    def plot_confound_correlation(self, fname=None, legend=True):
        """Plot how correlated the condition and confound regressors are."""
        corrs = self.design_matrix.corr()
        corrs = corrs.loc[self._confound_names, self._condition_names]

        n_bars = len(self._condition_names) * len(self._confound_names)
        xsize = min(n_bars * .2, 10)
        figsize = (xsize, 4)

        f, ax = plt.subplots(1, 1, figsize=figsize)
        n_conf = corrs.shape[0]
        colors = sns.husl_palette(n_conf)

        for i, (cond, conf_corrs) in enumerate(corrs.iteritems()):
            barpos = np.linspace(i, i + 1, n_conf + 1)[:-1]
            bars = ax.bar(barpos, conf_corrs.abs(), width=1 / n_conf,
                          color=colors, linewidth=0)

        ax.set_xticks(np.arange(len(self._condition_names)) + 0.5)
        ax.set_xticklabels(self._condition_names)
        ax.set_xlim(0, len(self._condition_names))
        ax.xaxis.grid(False)

        ymin, ymax = ax.get_ylim()
        ymax = max(.25, ymax)
        ax.set_ylim(0, ymax)
        ax.set_ylabel("abs(correlation)")

        for x in range(1, len(self._condition_names)):
            ax.axvline(x, ls=":", c="#222222", lw=1)

        if legend:
            ncol = len(self._confound_names) // 15 + 1
            box = ax.get_position()
            ax.set_position([box.x0, box.y0,
                             box.width * (1 - .15 * ncol), box.height])
            lgd = ax.legend(bars, self._confound_names, ncol=ncol, fontsize=10,
                            loc='center left', bbox_to_anchor=(1, 0.5))
        else:
            lgd = []

        if fname is not None:
            f.savefig(fname, bbox_extra_artists=[lgd], bbox_inches="tight")

    def plot_singular_values(self, fname=None):
        """Plot the singular values of the full design matrix."""
        s = self._singular_values
        smat = s * np.eye(len(s))

        size = min(.3 * len(s), 8)
        f, ax = plt.subplots(1, 1, figsize=(size, size))
        ax.matshow(smat, cmap="bone", zorder=2)
        ax.axis("off")

        plt.tight_layout()
        if fname is not None:
            f.savefig(fname)

    def to_csv(self, fname):
        """Save the full design matrix to csv."""
        self.design_matrix.to_csv(fname)

    def to_fsl_files(self, fstem, contrasts=None):
        """Save to FEAT-style {fstem}.mat and optionally {fstem}.con files."""
        m, n = self.design_matrix.shape
        header = "/NumWaves\t%d\n/NumPoints\t%d\n/PPheights\t\t%s\n"
        heights = "\t".join(["%7.7g" % p for p in self._pp_heights])
        header %= (n, m, heights)

        header += "\n/Matrix\n"

        sio = StringIO()
        self.design_matrix.to_csv(sio, "\t", float_format="%7.7g",
                                  header=False, index=False)

        with open(fstem + ".mat", "w") as fid:
            fid.write(header)
            fid.write(sio.getvalue())

        if contrasts is not None:
            n_cont = len(contrasts)
            names = [c[0] for c in contrasts]
            header = ""
            for i_name in enumerate(names, 1):
                header += "/ContrastName%d\t%s\n" % i_name

            header += "/NumWaves\t%d\n/NumContrasts\t%d\n" % (n, n_cont)
            header += "/PPheights\t\t\n/RequiredEffect\t\t\n\n/Matrix\n"

            Cs = []
            for _, names, weights in contrasts:
                Cs.append(self.contrast_vector(names, weights))
            C_all = np.array(Cs)

            sio = StringIO()
            np.savetxt(sio, C_all, fmt="%7.7g", delimiter=" ")

            with open(fstem + ".con", "w") as fid:
                fid.write(header)
                fid.write(sio.getvalue())

    def to_pickle(self, fname):
        """Save the object as a pickle to a file."""
        with open(fname, "w") as fid:
            cPickle.dump(self, fid)

    @classmethod
    def from_pickle(cls, fname):
        """Load an object from a pickled file."""
        with open(fname, "r") as fid:
            return cPickle.load(fid)

    @property
    def main_submatrix(self):
        """Conditions (no derivatives) and regressors."""
        return self.design_matrix[self._main_names]

    @property
    def condition_submatrix(self):
        """Only condition information."""
        return self.design_matrix[self._condition_names]

    @property
    def confound_submatrix(self):
        """Submatrix of confound regressors."""
        if not self._confound_names:
            return None
        return self.design_matrix[self._confound_names]

    @property
    def artifact_submatrix(self):
        """Submatrix of artifact regressors."""
        if not self._artifact_names:
            return None
        return self.design_matrix[self._artifact_names]

    @property
    def shape(self):
        """Shape of the full design matrix."""
        return self.design_matrix.shape


def fsl_highpass_matrix(ntp, cutoff, tr=2):
    """Return an array to implement FSL's gaussian running line filter.

    To implement the filter, premultiply your data with this array.

    Parameters
    ----------
    ntp : int
        number of observations in data
    cutoff : float
        filter cutoff in seconds
    tr : float
        TR of data in seconds

    Return
    ------
    F : ntp square array
        filter matrix

    """
    cutoff = cutoff / tr
    sig2n = np.square(cutoff / np.sqrt(2))

    kernel = np.exp(-np.square(np.arange(ntp)) / (2 * sig2n))
    kernel = 1 / np.sqrt(2 * np.pi * sig2n) * kernel

    K = sp.linalg.toeplitz(kernel)
    K = np.dot(np.diag(1 / K.sum(axis=1)), K)

    H = np.zeros((ntp, ntp))
    X = np.column_stack((np.ones(ntp), np.arange(ntp)))
    for k in range(ntp):
        W = np.diag(K[k])
        hat = np.dot(np.dot(X, np.linalg.pinv(np.dot(W, X))), W)
        H[k] = hat[k]
    F = np.eye(ntp) - H
    return F


def fsl_highpass_filter(data, cutoff=128, tr=2, copy=True):
    """Highpass filter data with gaussian running line filter.

    Parameters
    ----------
    data : 1d or 2d array
        data array where first dimension is observations
    cutoff : float
        filter cutoff in seconds
    tr : float
        data TR in seconds
    copy : boolean
        if False data is filtered in place

    Returns
    -------
    data : 1d or 2d array
        filtered version of the data

    """
    if copy:
        data = data.copy()
    # Ensure data is in right shape
    ntp = len(data)
    data = np.atleast_2d(data).reshape(ntp, -1)

    # Filter each column of the data
    F = fsl_highpass_matrix(ntp, cutoff, tr)
    data[:] = np.dot(F, data)

    return data.squeeze()
