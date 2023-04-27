"""Workflow to merge multiple datasets into a single dataset used for elevation and manning data."""
import logging
from typing import Dict, List, Union

import geopandas as gpd
import numpy as np
import xarray as xr
from scipy import ndimage

logger = logging.getLogger(__name__)

__all__ = ["merge_multi_dataarrays", "merge_dataarrays"]


def merge_multi_dataarrays(
    da_list: List[dict],
    da_like: xr.DataArray = None,
    reproj_kwargs: Dict = {},
    buffer_cells: int = 0,  # not in list
    interp_method: str = "linear",  # not in list
    logger=logger,
) -> xr.DataArray:
    """Merge a list of data arrays by reprojecting these to a common destination grid
    and combine valid values.

    Parameters
    ----------
    da_list : List[dict]
        list of dicts with xr.DataArrays and optional merge arguments.
        Possible merge arguments are:

        * reproj_method: str, optional
            Reprojection method, if not provided, method is based on resolution (average when resolution of destination grid is coarser then data reosltuion, else bilinear).
        * offset: xr.DataArray, float, optional
            Dataset with spatially varying offset or float with uniform offset
        * zmin, zmax : float, optional
            Range of valid elevations for da2 -  only valid cells are not merged.
            Note: applied after offset!
        * gdf_valid: gpd.GeoDataFrame, optional
            Geometry of the valid region for da2
    da_like : xr.Dataarray, optional
        Destination grid, by default None.
        If provided the output data is projected to this grid, otherwise to the first input grid.
    reproj_kwargs: dict, optional
        Keyword arguments for reprojecting the data to the destination grid. Only used of no da_like is provided.
    buffer_cells : int, optional
        Number of cells between datasets to ensure smooth transition of bed levels, by default 0
    interp_method : str, optional
        Interpolation method used to fill the buffer cells , by default "linear"

    Returns
    -------
    xr.DataArray
        merged data array

    See Also:
    ---------
    :py:func:`~hydromt_sfincs.workflows.merge.merge_dataarrays`

    """

    # start with common grid
    method = da_list[0].get("reproj_method", None)
    da1 = da_list[0].get("da")

    # get resolution of da1 in meters
    dx_1 = (
        np.abs(da1.raster.res[0])
        if not da1.raster.crs.is_geographic
        else np.abs(da1.raster.res[0]) * 111111.0
    )

    # if no reprojection method is specified, base method on resolutions
    # if resolution dataset >= resolution destination grid: bilinear
    # if resolution dataset < resolution destination grid: average

    if method is None and da_like is not None:
        dx_like = (
            np.abs(da_like.raster.res[0])
            if not da_like.raster.crs.is_geographic
            else np.abs(da_like.raster.res[0]) * 111111.0
        )
        if dx_1 >= dx_like:
            method = "bilinear"
        else:
            method = "average"
    else:
        method = "bilinear"

    if da_like is not None:  # reproject first raster to destination grid
        da1 = da1.raster.reproject_like(da_like, method=method).load()
    elif reproj_kwargs:
        da1 = da1.raster.reproject(method=method, **reproj_kwargs).load()
    logger.debug(f"Reprojection method of first dataset is: {method}")

    # set nodata to np.nan, Note this might change the dtype to float
    da1 = da1.raster.mask_nodata()

    # get valid cells of first dataset
    da1 = _add_offset_mask_invalid(
        da1,
        offset=da_list[0].get("offset", None),
        min_valid=da_list[0].get("zmin", None),
        max_valid=da_list[0].get("zmax", None),
        gdf_valid=da_list[0].get("gdf_valid", None),
        reproj_method="bilinear",  # always bilinear!
    )

    # combine with next dataset
    for i in range(1, len(da_list)):
        merge_method = da_list[i].get("merge_method", "first")
        if merge_method == "first" and not np.any(np.isnan(da1.values)):
            continue

        # base reprojection method on resolution of datasets
        reproj_method = da_list[i].get("reproj_method", None)
        da2 = da_list[i].get("da")
        if reproj_method is None:
            dx_2 = (
                np.abs(da2.raster.res[0])
                if not da2.raster.crs.is_geographic
                else np.abs(da2.raster.res[0]) * 111111.0
            )
            if dx_2 >= dx_1:
                reproj_method = "bilinear"
            else:
                reproj_method = "average"
        else:
            reproj_method = "bilinear"
        logger.debug(f"Reprojection method of dataset {str(i)} is: {method}")

        da1 = merge_dataarrays(
            da1,
            da2=da2,
            offset=da_list[i].get("offset", None),
            min_valid=da_list[i].get("zmin", None),
            max_valid=da_list[i].get("zmax", None),
            gdf_valid=da_list[i].get("gdf_valid", None),
            reproj_method=reproj_method,
            merge_method=merge_method,
            buffer_cells=buffer_cells,
            interp_method=interp_method,
        )

    return da1


def merge_dataarrays(
    da1: xr.DataArray,
    da2: xr.DataArray,
    offset: Union[xr.DataArray, float] = None,
    min_valid: float = None,
    max_valid: float = None,
    gdf_valid: gpd.GeoDataFrame = None,
    buffer_cells: int = 0,
    merge_method: str = "first",
    reproj_method: str = "bilinear",
    interp_method: str = "linear",
) -> xr.DataArray:
    """Return merged data from two data arrays.

    Valid cells of da2 are merged with da1 according to merge_method.
    Valid cells are based on its nodata value; the min_valid-max_valid range; and the gd_valid region.

    If `buffer` > 0, values at the interface between both data arrays
    are interpolate to create a smooth surface.

    If `offset` is provided, a (spatially varying) offset is added to the
    second dataset to convert the vertical datum before merging.

    Parameters
    ----------
    da1, da2: xr.DataArray
        Data arrays to be merged.
    offset: xr.DataArray, float, optional
        Dataset with spatially varying offset or float with uniform offset
    min_valid, max_valid : float, optional
        Range of valid values for da2 -  only valid cells are not merged.
        Note: applied after offset!
    gdf_valid: gpd.GeoDataFrame, optional
        Geometry of the valid region for da2
    buffer_cells: int, optional
        Buffer (number of cells) around valid cells in da1 (if `merge_method='first'`)
        or da2 (if `merge_method='last'`) where values are interpolated
        to create a smooth surface between both datasets, by default 0.
    merge_method: {'first','last', 'mean', 'max', 'min'}, optional
        merge method, by default 'first':
        * first: use valid new where existing invalid
        * last: use valid new
        * mean: use mean of valid new and existing
        * max: use max of valid new and existing
        * min: use min of valid new and existing
    reproj_method: {'bilinear', 'cubic', 'nearest', 'average', 'max', 'min'}
        Method used to reproject the offset and second dataset to the grid of the
        first dataset, by default 'bilinear'.
        See :py:meth:`rasterio.warp.reproject` for more methods
    interp_method, {'linear', 'nearest', 'rio_idw'}
        Method used to interpolate the buffer cells, by default 'linear'.

    Returns
    -------
    da_out: xr.DataArray
        Merged dataarray
    """

    nodata = da1.raster.nodata
    dtype = da1.dtype
    if not np.isnan(nodata):
        da1 = da1.raster.mask_nodata()
    ## reproject da2 and reset nodata value to match da1 nodata
    try:
        da2 = (
            da2.raster.reproject_like(da1, method=reproj_method)
            .raster.mask_nodata()
            .load()
        )
    except:
        print("No data for this tile")

    da2 = _add_offset_mask_invalid(
        da=da2,
        offset=offset,
        min_valid=min_valid,
        max_valid=max_valid,
        gdf_valid=gdf_valid,
        reproj_method="bilinear",  # always bilinear!
    )
    # merge based merge_method
    if merge_method == "first":
        mask = ~np.isnan(da1)
    elif merge_method == "last":
        mask = np.isnan(da2)
    elif merge_method == "mean":
        mask = np.isnan(da1)
        da2 = (da1 + da2) / 2
    elif merge_method == "max":
        mask = da1 >= da2
    elif merge_method == "min":
        mask = da1 <= da2
    else:
        raise ValueError(f"Unknown merge_method: {merge_method}")
    da_out = da1.where(mask, da2)
    da_out.raster.set_nodata(np.nan)
    # identify buffer cells and interpolate data
    if buffer_cells > 0 and interp_method:
        mask_dilated = ndimage.binary_dilation(
            mask, structure=np.ones((3, 3)), iterations=buffer_cells
        )
        mask_buf = np.logical_xor(mask, mask_dilated)
        da_out = da_out.where(~mask_buf, np.nan)
        da_out_interp = da_out.raster.interpolate_na(method=interp_method)
        da_out = da_out.where(~mask_buf, da_out_interp)

    da_out = da_out.fillna(nodata).astype(dtype)
    da_out.raster.set_nodata(nodata)
    return da_out


# Merge data for Curvenumber
def curvenumber_recovery_determination(da_landuse, da_HSG, da_Ksat, df_map, da_smax, da_kr):

    """Setup model the Soil Conservation Service (SCS) Curve Number (CN) files.
    More information see http://new.streamstech.com/wp-content/uploads/2018/07/SWMM-Reference-Manual-Part-I-Hydrology-1.pdf

    Parameters
    ----------
    dataset_landuse : filename (or Path) of gridded data with land use classes (e.g. NLCD)
    dataset_HSG     : filename (or Path) of gridded data with hydrologic soil group classes (HSG)
    dataset_Ksat    : filename (or Path) of gridded data with saturated hydraulic conductivity (Ksat)
    reclass_table   : mapping table that related landuse and HSG to each other (matrix; not list)
    """
    # Started
    print(' working on curve numbers')

    # Interpolate soil type to landuse
    da_HSG_to_landuse   = da_HSG.raster.reproject_like(da_landuse, method="nearest").load()

    # Curve numbers to grid: go over NLCD classes and HSG classes
    da_CN               = da_landuse
    da_CN               = da_CN.where(False, np.NaN)
    for i in range(len(df_map._stat_axis)):
        for j in range(len(df_map.columns)):
            ind                 = ((da_landuse == df_map._stat_axis[i]) & (da_HSG_to_landuse == int(df_map.columns[j]) ))
            da_CN               = da_CN.where(~ind, df_map.values[i,j])

    # Convert CN to maximum soil retention (S) model grid and interpolate
    da_CN               = np.maximum(da_CN, 0)                  # always positive
    da_CN               = np.minimum(da_CN, 100)                # not higher than 100
    da_s                = np.maximum(1000 / da_CN - 10, 0)      # Equation 4.41
    da_s                = da_s.fillna(0.0)                      # NaN means no infiltration = 0
    ind                 = np.isfinite(da_s)                     # inf values will be set to
    da_s                = da_s.where(ind, 0.0)                  # no infiltration
    da_s                = da_s* 0.0254                          # maximum value in meter (constant)

    # Interpolate Smax
    da_smax             = da_s.raster.reproject_like(da_smax, method="average").load()

    # Interpolate Ksat to grid, define recovery as percentage
    # Reference information fom Table 4.7
    # Very low 	    0 - 0.01               
    # Low 		    0.01 - 0.1      clay        0.07 µm/s   0.01 inch/hr    0.1%
    # Med-low 	    0.1 - 1         loam        0.9 µm/s    0.13 inch/hr    0.5%
    # med-high 	    1 - 10          Loamy sand  8.3 µm/s    1.18 inch/hr    1.4%
    # high 		    10 - 100        Sand        33 µm/s     4.74 inch/hr    2.9%
    # very high 	100 - Inf  
    da_kr               = da_Ksat.raster.reproject_like(da_kr, method="average").load()
    da_kr               = np.minimum(da_kr, 100)    # not higher than 100
    da_kr               = da_kr*0.141732;           # from micrometers per second to inch/hr    (constant)
    da_kr               = np.sqrt(da_kr)/75;        # recovery in percentage of Smax per hour   (Eq. 4.36)

    # Ensure no NaNs
    da_smax             = da_smax.fillna(0)
    da_kr               = da_kr.fillna(0)

    # Done
    return da_smax, da_kr


## Helper functions
def _add_offset_mask_invalid(
    da,
    offset=None,
    min_valid=None,
    max_valid=None,
    gdf_valid=None,
    reproj_method: str = "bilinear",
):
    ## add offset
    if offset is not None:
        if isinstance(offset, xr.DataArray):
            offset = (
                offset.raster.reproject_like(da, method=reproj_method)
                .raster.mask_nodata()
                .fillna(0)
            )
        da = da.where(np.isnan(da), da + offset)
    # mask invalid values before merging
    if min_valid is not None:
        da = da.where(da >= min_valid, np.nan)
    if max_valid is not None:
        da = da.where(da <= max_valid, np.nan)
    if gdf_valid is not None:
        da = da.where(da.raster.geometry_mask(gdf_valid), np.nan)
    return da
