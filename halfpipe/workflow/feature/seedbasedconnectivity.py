# -*- coding: utf-8 -*-
# emacs: -*- mode: python; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vi: set ft=python sts=4 ts=4 sw=4 et:

from nipype.pipeline import engine as pe
from nipype.interfaces import utility as niu
from nipype.algorithms import confounds as nac
from nipype.interfaces import fsl

from fmriprep import config

from ...interface import (
    MergeColumns,
    FillNA,
    MakeDofVolume,
    Resample,
    CalcMean,
    MakeResultdicts,
    ResultdictDatasink,
)

from ..memory import MemoryCalculator
from ..constants import constants
from ...utils import formatlikebids


def _contrasts(design_file=None):
    from pathlib import Path

    from halfpipe.io import loadspreadsheet
    import numpy as np
    import pandas as pd
    import csv

    design_df = loadspreadsheet(design_file)
    m, n = design_df.shape

    contrast_mat = np.zeros((1, n))
    contrast_mat[0, 0] = 1

    contrast_df = pd.DataFrame(
        contrast_mat, index=[design_df.columns[0]], columns=design_df.columns
    )

    out_with_header = Path.cwd() / "merge_with_header.tsv"
    contrast_df.to_csv(
        out_with_header,
        sep="\t",
        index=False,
        na_rep="n/a",
        header=True,
        quoting=csv.QUOTE_NONNUMERIC,
    )
    out_no_header = Path.cwd() / "merge_no_header.tsv"
    contrast_df.to_csv(out_no_header, sep="\t", index=False, na_rep="n/a", header=False)
    return str(out_with_header), str(out_no_header)


def init_seedbasedconnectivity_wf(
    workdir=None, feature=None, seed_files=None, seed_spaces=None, memcalc=MemoryCalculator()
):
    """
    create workflow to calculate seed connectivity maps
    """
    if feature is not None:
        name = f"{formatlikebids(feature.name)}_wf"
    else:
        name = "seedbasedconnectivity_wf"
    workflow = pe.Workflow(name=name)

    # input
    inputnode = pe.Node(
        niu.IdentityInterface(
            fields=[
                "tags",
                "bold",
                "mask",
                "confounds_selected",
                "seed_names",
                "seed_files",
                "seed_spaces",
            ]
        ),
        name="inputnode",
    )
    outputnode = pe.Node(niu.IdentityInterface(fields=["resultdicts"]), name="outputnode")

    if feature is not None:
        inputnode.inputs.seed_names = feature.seeds

    if seed_files is not None:
        inputnode.inputs.seed_files = seed_files

    if seed_spaces is not None:
        inputnode.inputs.seed_spaces = seed_spaces

    #
    statmaps = ["effect", "variance", "z", "dof", "mask"]
    make_resultdicts = pe.Node(
        MakeResultdicts(
            tagkeys=["feature", "seed"],
            imagekeys=[*statmaps, "design_matrix", "contrast_matrix"],
            metadatakeys=["mean_t_s_n_r"],
        ),
        name="make_resultdicts",
        run_without_submitting=True
    )
    if feature is not None:
        make_resultdicts.inputs.feature = feature.name
    workflow.connect(inputnode, "tags", make_resultdicts, "tags")
    workflow.connect(inputnode, "seed_names", make_resultdicts, "seed")

    workflow.connect(make_resultdicts, "resultdicts", outputnode, "resultdicts")

    #
    resultdict_datasink = pe.Node(
        ResultdictDatasink(base_directory=workdir), name="resultdict_datasink"
    )
    workflow.connect(make_resultdicts, "resultdicts", resultdict_datasink, "indicts")

    #
    reference_dict = dict(reference_space=constants.reference_space, reference_res=constants.reference_res)
    resample = pe.MapNode(
        Resample(interpolation="MultiLabel", **reference_dict),
        name="resample",
        iterfield=["input_image", "input_space"],
        n_procs=config.nipype.omp_nthreads,
        mem_gb=memcalc.series_std_gb,
    )
    workflow.connect(inputnode, "seed_files", resample, "input_image")
    workflow.connect(inputnode, "seed_spaces", resample, "input_space")

    # Delete zero voxels for the seeds
    applymask = pe.MapNode(
        fsl.ApplyMask(),
        name="applymask",
        iterfield="in_file",
        mem_gb=memcalc.volume_std_gb,
    )
    workflow.connect(inputnode, "mask", applymask, "mask_file")
    workflow.connect(resample, "output_image", applymask, "in_file")

    # calculate the mean time series of the region defined by each mask
    meants = pe.MapNode(
        fsl.ImageMeants(), name="meants", iterfield="mask", mem_gb=memcalc.series_std_gb,
    )
    workflow.connect(inputnode, "bold", meants, "in_file")
    workflow.connect(applymask, "out_file", meants, "mask")

    #
    design = pe.MapNode(MergeColumns(2), iterfield=["in1", "column_names1"], name="design", run_without_submitting=True)
    workflow.connect(meants, "out_file", design, "in1")
    workflow.connect(inputnode, "seed_names", design, "column_names1")
    workflow.connect(inputnode, "confounds_selected", design, "in2")

    workflow.connect(design, "out_with_header", make_resultdicts, "design_matrix")

    contrasts = pe.MapNode(
        niu.Function(
            input_names=["design_file"],
            output_names=["out_with_header", "out_no_header"],
            function=_contrasts,
        ),
        iterfield="design_file",
        name="contrasts",
        run_without_submitting=True
    )
    workflow.connect(design, "out_with_header", contrasts, "design_file")

    workflow.connect(contrasts, "out_with_header", make_resultdicts, "contrast_matrix")

    fillna = pe.MapNode(FillNA(), iterfield="in_tsv", name="fillna")
    workflow.connect(design, "out_no_header", fillna, "in_tsv")

    # calculate the regression of the mean time series
    # onto the functional image.
    # the result is the seed connectivity map
    glm = pe.MapNode(
        fsl.GLM(
            out_cope="cope.nii.gz",
            out_varcb_name="varcope.nii.gz",
            out_z_name="zstat.nii.gz",
            demean=True,
        ),
        name="glm",
        iterfield=["design", "contrasts"],
        mem_gb=memcalc.series_std_gb * 5,
    )
    workflow.connect(inputnode, "bold", glm, "in_file")
    workflow.connect(inputnode, "mask", glm, "mask")
    workflow.connect(fillna, "out_no_header", glm, "design")
    workflow.connect(contrasts, "out_no_header", glm, "contrasts")

    # make dof volume
    makedofvolume = pe.MapNode(MakeDofVolume(), iterfield=["design"], name="makedofvolume", run_without_submitting=True)
    workflow.connect(inputnode, "bold", makedofvolume, "bold_file")
    workflow.connect(fillna, "out_no_header", makedofvolume, "design")

    workflow.connect(glm, "out_cope", make_resultdicts, "effect")
    workflow.connect(glm, "out_varcb", make_resultdicts, "variance")
    workflow.connect(glm, "out_z", make_resultdicts, "z")
    workflow.connect(makedofvolume, "out_file", make_resultdicts, "dof")
    workflow.connect(inputnode, "mask", make_resultdicts, "mask")

    #
    tsnr = pe.Node(nac.TSNR(), name="tsnr", mem_gb=memcalc.series_std_gb)
    workflow.connect(inputnode, "bold", tsnr, "in_file")

    calcmean = pe.Node(CalcMean(), name="calcmean", mem_gb=memcalc.series_std_gb)
    workflow.connect(resample, "output_image", calcmean, "mask")
    workflow.connect(tsnr, "tsnr_file", calcmean, "in_file")

    workflow.connect(calcmean, "mean", make_resultdicts, "mean_t_s_n_r")

    #
    # mergesources = pe.MapNode(niu.Merge(4), iterfield="in3", name="mergesources")
    # workflow.connect(inputnode, "bold", mergesources, "in1")
    # workflow.connect(inputnode, "mask", mergesources, "in2")
    # workflow.connect(resample, "output_image", mergesources, "in3")
    #
    # workflow.connect(mergesources, "out", make_resultdicts, "sources")

    return workflow
