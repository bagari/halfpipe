# -*- coding: utf-8 -*-
# emacs: -*- mode: python; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vi: set ft=python sts=4 ts=4 sw=4 et:

import numpy as np
import pandas as pd

from .base import ResultdictsOutputSpec
from ...io import ExcludeDatabase, loadspreadsheet
from ...model import ResultdictSchema

from nipype.interfaces.base import (
    traits,
    BaseInterfaceInputSpec,
    SimpleInterface,
    isdefined,
    File
)


def _aggregate_if_needed(inval):
    if isinstance(inval, (list, tuple)):
        return np.asarray(inval).mean()
    return float(inval)


def _get_categorical_dict(filepath, variabledicts):
    rawdataframe = loadspreadsheet(filepath)
    for variabledict in variabledicts:
        if variabledict.get("type") == "id":
            id_column = variabledict.get("name")
            break

    rawdataframe[id_column] = pd.Series(rawdataframe[id_column], dtype=str)
    if all(str(id).startswith("sub-") for id in rawdataframe[id_column]):  # for bids
        rawdataframe[id_column] = [str(id).replace("sub-", "") for id in rawdataframe[id_column]]
    rawdataframe = rawdataframe.set_index(id_column)

    categorical_columns = []
    for variabledict in variabledicts:
        if variabledict.get("type") == "categorical":
            categorical_columns.append(variabledict.get("name"))

    return rawdataframe[categorical_columns].to_dict()


class FilterResultdictsInputSpec(BaseInterfaceInputSpec):
    indicts = traits.List(traits.Dict(traits.Str(), traits.Any()), mandatory=True)
    filterdicts = traits.List(traits.Any(), desc="filter list", mandatory=True)
    variabledicts = traits.List(traits.Any(), desc="variable list")
    spreadsheet = File(desc="spreadsheet")
    requireoneofimages = traits.List(
        traits.Str(), desc="only keep resultdicts that have at least one of these keys"
    )
    excludefiles = traits.List(File())


class FilterResultdicts(SimpleInterface):
    input_spec = FilterResultdictsInputSpec
    output_spec = ResultdictsOutputSpec

    def _run_interface(self, runtime):
        outdicts = self.inputs.indicts.copy()

        resultdict_schema = ResultdictSchema()
        outdicts = [resultdict_schema.load(outdict) for outdict in outdicts]  # validate

        categorical_dict = None

        for filterdict in self.inputs.filterdicts:
            action = filterdict.get("action")

            filtertype = filterdict.get("type")
            if filtertype == "group":
                if categorical_dict is None:
                    assert isdefined(self.inputs.spreadsheet)
                    assert isdefined(self.inputs.variabledicts)
                    categorical_dict = _get_categorical_dict(
                        self.inputs.spreadsheet, self.inputs.variabledicts
                    )

                variable = filterdict.get("variable")
                if variable not in categorical_dict:
                    continue

                levels = filterdict.get("levels")
                if levels is None or len(levels) == 0:
                    continue

                variable_dict = categorical_dict[variable]
                selectedsubjects = set(
                    subject for subject, value in variable_dict.items() if value in levels
                )

                if action == "include":
                    outdicts = [
                        outdict
                        for outdict in outdicts
                        if outdict.get("tags").get("sub") in selectedsubjects
                    ]
                elif action == "exclude":
                    outdicts = [
                        outdict
                        for outdict in outdicts
                        if outdict.get("tags").get("sub") not in selectedsubjects
                    ]
                else:
                    raise ValueError(f'Invalid action "{action}"')

            elif filtertype == "cutoff":

                assert action == "exclude"

                cutoff = filterdict.get("cutoff")
                if cutoff is None or not isinstance(cutoff, float):
                    raise ValueError(f'Invalid cutoff "{cutoff}"')

                filterfield = filterdict.get("field")
                outdicts = [
                    outdict
                    for outdict in outdicts
                    if _aggregate_if_needed(outdict.get("vals").get(filterfield, np.inf)) < cutoff
                ]

        if isdefined(self.inputs.requireoneofimages):
            requireoneofimages = self.inputs.requireoneofimages
            if len(requireoneofimages) > 0:
                outdicts = [
                    outdict
                    for outdict in outdicts
                    if any(
                        requireoneofkey in outdict.get("images")
                        for requireoneofkey in requireoneofimages
                    )
                ]

        if isdefined(self.inputs.excludefiles):
            database = ExcludeDatabase.cached(self.inputs.excludefiles)
            outdicts = [
                outdict for outdict in outdicts if database.get(**outdict.get("tags")) is False
            ]

        self._results["resultdicts"] = outdicts

        return runtime