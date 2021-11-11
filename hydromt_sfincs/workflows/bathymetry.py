import geopandas as gpd
import numpy as np
import pyflwdir
import xarray as xr
from scipy import ndimage
from typing import Union, Tuple, Optional
import logging

from hydromt.gis_utils import nearest_merge, nearest, spread2d
from hydromt.workflows import rivers

logger = logging.getLogger(__name__)

__all__ = [
    "merge_topobathy",
    "mask_topobathy",
    "get_rivbank_dz",
    "get_river_zb",
    "burn_river_zb",
]


def mask_topobathy(
    da_elv: xr.DataArray, elv_min: float, elv_max: float = None
) -> xr.DataArray:
    """Return mask of valid elevation cells within [elv_min, elv_max] range.
    Note that local sinks (isolated regions with elv < elv_min) are kept.
    """
    dep_mask = da_elv != da_elv.raster.nodata
    if elv_min is not None:
        # active cells: contiguous area above depth threshold
        _msk = ndimage.binary_fill_holes(da_elv >= elv_min)
        dep_mask = dep_mask.where(_msk, False)

    if elv_max is not None:
        dep_mask = dep_mask.where(da_elv <= elv_max, False)

    return dep_mask


def merge_topobathy(
    da1: xr.DataArray,
    da2: xr.DataArray,
    da_offset: Union[xr.DataArray, float] = None,
    merge_buffer: int = 0,
    merge_method: str = "first",
    elv_min: float = None,
    elv_max: float = None,
    reproj_method: str = "bilinear",
    logger=logger,
) -> xr.DataArray:
    """Return merged topobathy data from two datasets.

    Values from the second dataset are used where `da_mask` equals True or,
    if not provided, where the first dataset has missing values.

    If `merge_buffer` > 0, values of da2 are replaced with linearly
    interpolated values within the buffer.

    If `da_offset` is provided, a (spatially varying) offset is applied to the
    second dataset to convert the vertical datum before merging.

    Parameters
    ----------
    da1, da2: xr.DataArray
        Datasets with topobathy data to be merged.
    da_offset: xr.DataArray, float, optional
        Dataset with spatially varying offset or float with uniform offset
    merge_method: str {'first','last','min','max'}
        merge method, by default 'first':

        * first: use valid new where existing invalid
        * last: use valid new
        * min: pixel-wise min of existing and new
        * max: pixel-wise max of existing and new
    elv_min, elv_max : float, optional
        Minimum and maximum elevation caps for new topobathy cells, cells outside
        this range are linearly interpolated. Note: applied after offset!
    merge_buffer: int
        Buffer (number of cells) within the da_mask==True region where topobathy
        values are based on linear interpolation for a smooth transition, by default 0.
    reproj_method: str
        Method used to reproject the offset and second dataset to the grid of the
        first dataset, by default 'bilinear'

    Returns
    -------
    da_out: xr.DataArray
        Merged topobathy dataset
    """

    nodata = da1.raster.nodata
    if nodata is None or np.isnan(nodata):
        raise ValueError("da1 nodata value should be a finite value.")
    ## reproject da2 and reset nodata value to match da1 nodata
    da2 = (
        da2.raster.reproject_like(da1, method=reproj_method)
        .raster.mask_nodata()
        .fillna(nodata)
    )
    ## add offset
    if da_offset is not None:
        if isinstance(da_offset, xr.DataArray):
            da_offset = (
                da_offset.raster.reproject_like(da1, method=reproj_method)
                .raster.mask_nodata()
                .fillna(0)
            )
        da2 = da2.where(da2 == nodata, da2 + da_offset)
    # merge based merge_method
    if merge_method == "first":
        mask = da1 != nodata
    elif merge_method == "last":
        mask = da2 == nodata
    elif merge_method == "min":
        mask = da1 < da2
    elif merge_method == "max":
        mask = da1 > da2
    else:
        raise ValueError(f"Unknown merge_method: {merge_method}")
    da_out = da1.where(mask, da2)
    da_out.raster.set_nodata(nodata)
    # identify holes in merged elevation
    struct = np.ones((3, 3))
    na_mask = da_out != nodata
    if np.any(~na_mask.values):
        na_mask = ndimage.binary_fill_holes(na_mask, structure=struct)
    # mask invalid elevation values
    if elv_min is not None:
        da_out = da_out.where(~np.logical_and(~mask, da2 < elv_min), nodata)
    if elv_max is not None:
        da_out = da_out.where(~np.logical_and(~mask, da2 > elv_max), nodata)
    # identify buffer cells and set to nodata
    if merge_buffer > 0:
        mask_dilated = ndimage.binary_dilation(mask, struct, iterations=merge_buffer)
        mask_buf = np.logical_xor(mask, mask_dilated)
        da_out = da_out.where(~mask_buf, nodata)
    # interpolate invalid elevtn, buffer and holes ( nodata values )
    nempty = np.sum(da_out.values[na_mask] == nodata)
    if nempty > 0:
        logger.debug(f"Interpolate topobathy at {int(nempty)} cells")
        da_out = da_out.raster.interpolate_na(method="linear")
        da_out = da_out.where(na_mask, nodata)  # reset extrapolated area
    return da_out


def get_rivbank_dz(
    gdf_riv: gpd.GeoDataFrame,
    da_msk: xr.DataArray,
    da_hnd: xr.DataArray,
    nmin: int = 20,
    q: float = 25.0,
) -> np.ndarray:
    """Return river bank height estimated as from height above nearest drainage
    (HAND) values adjecent to river cells. For each feature in `gdf_riv` the nearest
    river bank cells are identified and the bank heigth is estimated based on a quantile
    value `q`.

    Parameters
    ----------
    gdf_riv : gpd.GeoDataFrame
        River segments
    da_msk : xr.DataArray of bool
        River mask
    da_hnd : xr.DataArray of float
        Height above nearest drain (HAND) map
    nmin : int, optional
        Minimum threshold for valid river bank cells, by default 20
    q : float, optional
        quantile [0-100] for river bank estimate, by default 25.0

    Returns
    -------
    rivbank_dz: np.ndarray
        riverbank elevations for each segment in `gdf_riv`
    da_riv_mask, da_bnk_mask: xr.DataArray:
        River and river-bank masks
    """
    # rasterize streams
    gdf_riv["segid"] = np.arange(1, gdf_riv.index.size + 1, dtype=np.int32)
    segid = da_hnd.raster.rasterize(gdf_riv, "segid").astype(np.int32)
    segid.raster.set_nodata(0)
    segid.name = "segid"
    # NOTE: the assumption is that banks are found in cells adjacent to any da_msk cell
    da_msk = da_msk.raster.reproject_like(da_hnd, method="nearest")
    _mask = ndimage.binary_fill_holes(da_msk)  # remove islands
    mask = ndimage.binary_dilation(_mask, np.ones((3, 3)))
    da_mask = xr.DataArray(
        coords=da_hnd.raster.coords, dims=da_hnd.raster.dims, data=mask
    )
    da_mask.raster.set_crs(da_hnd.raster.crs)
    # find nearest stream segment for all river bank cells
    segid_spread = spread2d(da_obs=segid, da_mask=da_mask)
    # get edge of riv mask -> riv banks
    da_bnk_mask = np.logical_and(da_hnd > 0, np.logical_xor(da_mask, _mask))
    da_riv_mask = np.logical_and(
        np.logical_and(da_hnd >= 0, da_msk), np.logical_xor(da_bnk_mask, da_mask)
    )
    # get median HAND for each stream -> riv bank dz
    rivbank_dz = ndimage.labeled_comprehension(
        da_hnd.values,
        labels=np.where(da_bnk_mask, segid_spread["segid"].values, np.int32(0)),
        index=gdf_riv["segid"].values,
        func=lambda x: 0 if x.size < nmin else np.percentile(x, q),
        out_dtype=da_hnd.dtype,
        default=0,
    )
    return rivbank_dz, da_riv_mask, da_bnk_mask


def get_river_zb(
    ds: xr.Dataset,
    flwdir: pyflwdir.FlwdirRaster,
    gdf_riv: gpd.GeoDataFrame = None,
    gdf_qbf: gpd.GeoDataFrame = None,
    method: str = "gvf",
    river_upa: float = 100.0,
    segment_length: float = 5e3,
    smooth_length: float = 10e3,
    min_convergence: float = 0.01,
    max_dist: float = 100.0,
    bankq: float = 25,
    adjust_estuary: bool = True,
    adjust_rivwth: bool = True,
    adjust_dem: bool = True,
    elevtn_name: str = "elevtn",
    uparea_name: str = "uparea",
    rivmsk_name: str = "rivmsk",
    logger=logger,
    **kwargs,
) -> Tuple[gpd.GeoDataFrame, xr.DataArray]:
    """Estimate river bedlevel zb using gradually varying flow (gvf), manning's equation
    (manning) or a power-law relation (powlaw) method. The river is based on flow
    directions with and minimum upstream area threshold.

    Parameters
    ----------
    ds : xr.Dataset
        Model map layers containing `elevnt_name`, `uparea_name` and `rivmsk_name` (optional)
        variables.
    flwdir : pyflwdir.FlwdirRaster
        Flow direction object
    gdf_riv : gpd.GeoDataFrame, optional
        River attribute data with "qbankfull" and "rivwth" data, by default None
    gdf_qbf : gpd.GeoDataFrame, optional
        Bankfull river discharge data with "qbankfull column", by default None
    method : {'gvf', 'manning', 'powlaw'}
        River bed estimate method, by default 'gvf'
    river_upa : float, optional
        Minumum upstream area threshold for rivers [km2], by default 100.0
    segment_length : float, optional
        Approximate river segment length [m], by default 5e3
    smooth_length : float, optional
        Approximate smooting length [m], by default 10e3
    min_convergence : float, optional
        Minimum width convergence threshold to define estuaries [m/m], by default 0.01
    max_dist : float, optional
        Maximum distance threshold to spatially merge `gdf_riv` and `gdf_qbf`, by default 100.0
    bankq : float, optional
        Quantile [1-100] for river bank estimation, by default 25.0
    adjust_estuary : bool, optional
        If True (default) fix the river depth in estuaries based on the upstream river depth.
    adjust_rivwth : bool, optional
        If True (default) calculate the river width based on the segment average width
        at the model resolution.
    adjust_dem : bool, optional
        If True (default) correct the river bed level to be hydrologically correct

    Returns
    -------
    gdf_riv: gpd.GeoDataFrame
        River segments with bed level (bz) estimates
    da_msk: xr.DataArray:
        River mask
    """
    raster_kwargs = dict(coords=ds.raster.coords, dims=ds.raster.dims)
    da_elv = ds[elevtn_name]

    # get vector of stream segments
    da_upa = ds[uparea_name]
    rivd8 = da_upa > river_upa
    feats = flwdir.streams(
        max_len=int(round(segment_length / ds.raster.res[0])),
        uparea=da_upa.values,
        elevtn=da_elv.values,
        rivdst=flwdir.distnc,
        strord=flwdir.stream_order(mask=rivd8),
        mask=rivd8,
    )
    gdf_stream = gpd.GeoDataFrame.from_features(feats, crs=ds.raster.crs)
    flw = pyflwdir.from_dataframe(gdf_stream.set_index("idx"))
    _ = flw.main_upstream(uparea=gdf_stream["uparea"].values)

    # merge gdf_riv with gdf_stream
    if gdf_riv is not None:
        cols = [c for c in ["rivwth", "qbankfull"] if c in gdf_riv]
        gdf_riv = nearest_merge(gdf_stream, gdf_riv, columns=cols, max_dist=max_dist)
        gdf_riv["rivlen"] = gdf_riv["rivdst"] - flw.downstream(gdf_riv["rivdst"])
    else:
        gdf_riv = gdf_stream
    # merge gdf_qbf (qbankfull) with gdf_riv
    if gdf_qbf is not None and "qbankfull" in gdf_qbf.colmns:
        if "qbankfull" in gdf_riv:
            gdf_riv = gdf_riv.drop(colums="qbankfull")
        gdf_riv = nearest_merge(gdf_riv, gdf_qbf, columns=cols, max_dist=max_dist)
    assert "qbankfull" in gdf_riv.columns, 'gdf_riv has no "qbankfull" data'
    check_rivwth = method == "powlaw" or adjust_rivwth or "rivwth" in gdf_riv.columns
    assert check_rivwth, 'gdf_riv has no "rivwth" data'
    # propagate qbankfull and rivwth values
    for col in ["qbankfull", "rivwth"]:
        if col not in gdf_riv.columns:
            continue
        data = gdf_riv[col].fillna(-9999)
        data = flw.fillnodata(data, -9999, direction="down", how="max")
        gdf_riv[col] = np.maximum(0, data)

    # create river mask with river polygon
    if rivmsk_name not in ds and "rivwth" in gdf_riv:
        assert gdf_riv.crs.is_projected
        gdf_riv_buf = gdf_riv.copy()
        buf = np.maximum(gdf_riv_buf["rivwth"] / 2, 1)
        gdf_riv_buf["geometry"] = gdf_riv_buf.buffer(buf)
        da_msk = np.logical_and(
            ds.raster.geometry_mask(gdf_riv), da_elv != da_elv.raster.nodata
        )
    elif rivmsk_name in ds:  #  merge river mask with river line
        da_msk = ds.raster.geometry_mask(gdf_riv, all_touched=True)
        da_msk = np.logical_or(da_msk, ds[rivmsk_name])
    else:
        raise ValueError("No river width or river mask provided.")

    ## get zs
    smooth_n = int(np.round(smooth_length / segment_length / 2))
    logger.info("Deriving bankfull river surface elevation profile.")
    da_hnd = xr.DataArray(flwdir.hand(rivd8.values, da_elv.values), **raster_kwargs)
    da_hnd.raster.set_crs(ds.raster.crs)
    rivbank_dz = get_rivbank_dz(gdf_riv, da_msk=da_msk, da_hnd=da_hnd, q=bankq)[0]
    gdf_riv["zs0"] = gdf_riv["elevtn"] + rivbank_dz
    zs0 = flw.dem_adjust(flw.moving_average(gdf_riv["zs0"], n=smooth_n))
    gdf_riv["zs"] = np.maximum(gdf_riv["elevtn"], zs0)
    gdf_riv["rivbank_dz"] = gdf_riv["zs"] - gdf_riv["elevtn"]

    # estimate stream segment average width from river mask
    if adjust_rivwth:
        logger.info("Deriving river segment average width.")
        rivwth = rivers.river_width(gdf_riv, da_rivmask=da_msk)
        gdf_riv["rivwth"] = flw.fillnodata(rivwth, -9999, direction="down", how="max")
        gdf_riv["rivwth"] = np.maximum(gdf_riv["rivwth"], 0)

    # estimate river depth, smooth and correct
    gdf_riv["rivdph0"] = rivers.river_depth(
        data=gdf_riv, flwdir=flw, method=method, **kwargs
    )
    gdf_riv["rivdph"] = flw.moving_average(gdf_riv["rivdph0"], n=smooth_n)

    if adjust_estuary:
        # set width from mask and depth constant in estuaries
        # estuaries based on convergence of width from river mask
        gdf_riv["estuary"] = flw.classify_estuaries(
            elevtn=gdf_riv["elevtn"],
            rivwth=flw.moving_average(gdf_riv["rivwth"], n=smooth_n),
            rivdst=gdf_riv["rivdst"],
            min_convergence=min_convergence,
        )
        rivdph = np.where(gdf_riv["estuary"] == 1, -9999, gdf_riv["rivdph"].values)
        gdf_riv["rivdph"] = flw.fillnodata(rivdph, -9999, "down")

    # calculate bed level from river depth
    gdf_riv["zb"] = gdf_riv["zs"] - gdf_riv["rivdph"]
    if adjust_dem:
        gdf_riv["zb"] = flw.dem_adjust(gdf_riv["zb"])
    gdf_riv["zb"] = np.minimum(gdf_riv["zb"], gdf_riv["elevtn"])
    gdf_riv["rivdph"] = gdf_riv["zs"] - gdf_riv["zb"]

    # calculate rivslp
    dz = gdf_riv["zb"] - flw.downstream(gdf_riv["zb"])
    dx = gdf_riv["rivdst"] - flw.downstream(gdf_riv["rivdst"])
    gdf_riv["rivslp"] = (dz / dx).fillna(0)

    return gdf_riv, da_msk


def burn_river_zb(
    gdf_riv: gpd.GeoDataFrame,
    da_elv: xr.DataArray,
    da_msk: xr.DataArray,
    flwdir: pyflwdir.FlwdirRaster = None,
    adjust_dem: bool = True,
    logger=logger,
):
    """Burn bedlevels from `gdf_riv` (column zb) into the DEM `da_elv` at river cells
    indicated in `da_msk`. The resulting river cells have D4 connectivity if `adjust_dem`.

    Parameters
    ----------
    gdf_riv: gpd.GeoDataFrame
        River segments with bed level (bz) estimates
    da_elv : xr.DataArray of float
        Elevation raster
    da_msk: xr.DataArray of bool:
        River mask
    flwdir : pyflwdir.FlwdirRaster, optional
        Flow direction object
    adjust_dem : bool, optional
        If True (default) ensure river cells have D4 connectivity

    Returns
    -------
    da_elv1: xr.DataArray
        DEM with bedlevels burned in.
    """
    assert da_elv.raster.identical_grid(da_msk)
    logger.debug("Burn bedlevel values into DEM.")
    nodata = da_elv.raster.nodata
    zb = da_elv.raster.rasterize(gdf_riv, col_name="zb", nodata=0)
    # interpolate values if rivslp and rivdst is given
    if np.all(np.isin(["rivslp", "rivdst"], gdf_riv.columns)) and flwdir is not None:
        logger.debug("Interpolate bedlevel values")
        gdf_riv1 = gdf_riv[gdf_riv["rivdst"] > 0]
        slp = da_elv.raster.rasterize(gdf_riv1, col_name="rivslp", nodata=0)
        dst0 = da_elv.raster.rasterize(gdf_riv1, col_name="rivdst", nodata=0)
        dst0 = np.where(dst0 > 0, flwdir.distnc - dst0, 0)
        zb = zb + dst0 * slp
    zb.raster.set_nodata(nodata)
    zb.name = "elevtn"
    # spread values inside river mask and replace da_elv values
    da_elv1 = spread2d(zb, da_msk)["elevtn"].where(da_msk, da_elv)
    da_elv1 = np.minimum(da_elv, da_elv1)
    da_elv1 = da_elv1.where(da_elv != nodata, nodata)

    if adjust_dem and flwdir is not None:
        logger.debug("Correct for D4 connectivity bed level")
        elevtn = flwdir.dem_adjust(da_elv1.values)  # NOTE: can we skip this?
        elevtn = flwdir.dem_dig_d4(elevtn, da_msk.values, nodata=nodata)
        da_elv1 = xr.DataArray(
            data=elevtn,
            coords=da_elv.raster.coords,
            dims=da_elv.raster.dims,
        ).where(da_msk, da_elv)

    # set attrs and return
    da_elv1.raster.set_nodata(nodata)
    da_elv1.raster.set_crs(da_elv.raster.crs)
    return da_elv1
