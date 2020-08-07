# -*- coding: utf-8 -*-
# emacs: -*- mode: python; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vi: set ft=python sts=4 ts=4 sw=4 et:

import nipype.pipeline.engine as pe
import nipype.interfaces.utility as niu
from nipype.interfaces import afni

from ...interface import MakeResultdicts, ResultdictDatasink, BlurInMask, ZScore

from ..memory import MemoryCalculator
from ...utils import formatlikebids


def init_falff_wf(workdir=None, feature=None, fwhm=None, memcalc=MemoryCalculator()):
    """
    Calculate Amplitude of low frequency oscillations(ALFF) and
    fractional ALFF maps

    Returns
    -------
    workflow : workflow object
        ALFF workflow

    Notes
    -----
    Adapted from
    <https://github.com/FCP-INDI/C-PAC/blob/master/CPAC/alff/alff.py>

    """
    if feature is not None:
        name = f"{formatlikebids(feature.name)}"
    else:
        name = "falff"
    if fwhm is not None:
        name = f"{name}_{int(float(fwhm) * 1e3):d}"
    name = f"{name}_wf"
    workflow = pe.Workflow(name=name)

    # input
    inputnode = pe.Node(
        niu.IdentityInterface(fields=["tags", "bold", "mask", "fwhm"]), name="inputnode",
    )
    unfiltered_inputnode = pe.Node(
        niu.IdentityInterface(fields=["bold", "mask"]), name="unfiltered_inputnode",
    )
    outputnode = pe.Node(niu.IdentityInterface(fields=["resultdicts"]), name="outputnode")

    if fwhm is not None:
        inputnode.inputs.fwhm = float(fwhm)

    #
    make_resultdicts = pe.Node(
        MakeResultdicts(imagekeys=["alff", "falff"]), name="make_resultdicts", run_without_submitting=True
    )
    workflow.connect(inputnode, "tags", make_resultdicts, "tags")

    workflow.connect(make_resultdicts, "resultdicts", outputnode, "resultdicts")

    #
    resultdict_datasink = pe.Node(
        ResultdictDatasink(base_directory=workdir), name="resultdict_datasink"
    )
    workflow.connect(make_resultdicts, "resultdicts", resultdict_datasink, "indicts")

    # standard deviation of the filtered image
    stddev_filtered = pe.Node(afni.TStat(), name="stddev_filtered", mem_gb=memcalc.series_std_gb)
    stddev_filtered.inputs.outputtype = "NIFTI_GZ"
    stddev_filtered.inputs.options = "-stdev"
    workflow.connect(inputnode, "bold", stddev_filtered, "in_file")
    workflow.connect(inputnode, "mask", stddev_filtered, "mask")

    # standard deviation of the unfiltered image
    stddev_unfiltered = pe.Node(
        afni.TStat(), name="stddev_unfiltered", mem_gb=memcalc.series_std_gb
    )
    stddev_unfiltered.inputs.outputtype = "NIFTI_GZ"
    stddev_unfiltered.inputs.options = "-stdev"
    workflow.connect(unfiltered_inputnode, "bold", stddev_unfiltered, "in_file")
    workflow.connect(unfiltered_inputnode, "mask", stddev_unfiltered, "mask")

    falff = pe.Node(afni.Calc(), name="falff", mem_gb=memcalc.volume_std_gb)
    falff.inputs.args = "-float"
    falff.inputs.expr = "(1.0*bool(a))*((1.0*b)/(1.0*c))"
    falff.inputs.outputtype = "NIFTI_GZ"
    workflow.connect(inputnode, "mask", falff, "in_file_a")
    workflow.connect(stddev_filtered, "out_file", falff, "in_file_b")
    workflow.connect(stddev_unfiltered, "out_file", falff, "in_file_c")

    #
    merge = pe.Node(niu.Merge(2), name="merge", run_without_submitting=True)
    workflow.connect(stddev_filtered, "out_file", merge, "in1")
    workflow.connect(falff, "out_file", merge, "in2")

    smooth = pe.MapNode(
        BlurInMask(preserve=True, float_out=True, out_file="blur.nii.gz"), iterfield="in_file", name="smooth"
    )
    workflow.connect(merge, "out", smooth, "in_file")
    workflow.connect(inputnode, "mask", smooth, "mask")
    workflow.connect(inputnode, "fwhm", smooth, "fwhm")

    zscore = pe.MapNode(ZScore(), iterfield="in_file", name="zscore", mem_gb=memcalc.volume_std_gb)
    workflow.connect(smooth, "out_file", zscore, "in_file")
    workflow.connect(inputnode, "mask", zscore, "mask")

    split = pe.Node(niu.Split(splits=[1, 1]), name="split", run_without_submitting=True)
    workflow.connect(zscore, "out_file", split, "inlist")

    workflow.connect(split, "out1", make_resultdicts, "alff")
    workflow.connect(split, "out2", make_resultdicts, "falff")

    #
    # mergesources = pe.Node(niu.Merge(2), name="mergesources")
    # workflow.connect(inputnode, "bold", mergesources, "in1")
    # workflow.connect(inputnode, "mask", mergesources, "in2")
    #
    # workflow.connect(mergesources, "out", make_resultdicts, "sources")

    return workflow
