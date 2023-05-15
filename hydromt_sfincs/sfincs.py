"""
SfincsModel class
"""
from __future__ import annotations

import glob
import logging
import os
from os.path import abspath, basename, dirname, isabs, isfile, join
from pathlib import Path
from typing import Any, Dict, List, Tuple, Union

import geopandas as gpd
import hydromt
import numpy as np
import pandas as pd
import xarray as xr
from hydromt.models.model_grid import GridModel
from hydromt.raster import RasterDataArray
from hydromt.vector import GeoDataArray, GeoDataset
from pyproj import CRS
from shapely.geometry import box, LineString, MultiLineString

from . import DATADIR, plots, utils, workflows
from .regulargrid import RegularGrid
from .sfincs_input import SfincsInput

__all__ = ["SfincsModel"]

logger = logging.getLogger(__name__)


class SfincsModel(GridModel):
    # GLOBAL Static class variables that can be used by all methods within
    # SfincsModel class. Typically list of variables (e.g. _MAPS) or
    # dict with varname - filename pairs (e.g. thin_dams : thd)
    _NAME = "sfincs"
    _GEOMS = {
        "observation_points": "obs",
        "weirs": "weir",
        "thin_dams": "thd",
        "drainage_structures": "drn",
    }  # parsed to dict of geopandas.GeoDataFrame
    _FORCING_1D = {
        # timeseries (can be multiple), locations tuple
        "waterlevel": (["bzs"], "bnd"),
        "waves": (["bzi"], "bnd"),
        "discharge": (["dis"], "src"),
        "precip": (["precip"], None),
        "wavespectra": (["bhs", "btp", "bwd", "bds"], "bwv"),
        "wavemaker": (["whi", "wti", "wst"], "wvp"),  # TODO check names and test
    }
    _FORCING_NET = {
        # 2D forcing sfincs name, rename tuple
        "waterlevel": ("netbndbzsbzi", {"zs": "bzs", "zi": "bzi"}),
        "discharge": ("netsrcdis", {"discharge": "dis"}),
        "precip": ("netampr", {"Precipitation": "precip"}),
        "press": ("netamp", {"barometric_pressure": "press"}),
        "wind": ("netamuamv", {"eastward_wind": "wind_u", "northward_wind": "wind_v"}),
    }
    _FORCING_SPW = {"spiderweb": "spw"}  # TODO add read and write functions
    _MAPS = ["msk", "dep", "scs", "manning", "qinf"]
    _STATES = ["rst", "ini"]
    _FOLDERS = []
    _CLI_ARGS = {"region": "setup_grid_from_region", "res": "setup_grid_from_region"}
    _CONF = "sfincs.inp"
    _DATADIR = DATADIR
    _ATTRS = {
        "dep": {"standard_name": "elevation", "unit": "m+ref"},
        "msk": {"standard_name": "mask", "unit": "-"},
        "scs": {
            "standard_name": "potential maximum soil moisture retention",
            "unit": "in",
        },
        "qinf": {"standard_name": "infiltration rate", "unit": "mm.hr-1"},
        "manning": {"standard_name": "manning roughness", "unit": "s.m-1/3"},
        "bzs": {"standard_name": "waterlevel", "unit": "m+ref"},
        "bzi": {"standard_name": "wave height", "unit": "m"},
        "dis": {"standard_name": "discharge", "unit": "m3.s-1"},
        "precip": {"standard_name": "precipitation", "unit": "mm.hr-1"},
    }

    def __init__(
        self,
        root: str = None,
        mode: str = "w",
        config_fn: str = "sfincs.inp",
        write_gis: bool = True,
        data_libs: Union[List[str], str] = None,
        logger=logger,
    ):
        """
        The SFINCS model class (SfincsModel) contains methods to read, write, setup and edit
        `SFINCS <https://sfincs.readthedocs.io/en/latest/>`_ models.

        Parameters
        ----------
        root: str, Path, optional
            Path to model folder
        mode: {'w', 'r+', 'r'}
            Open model in write, append or reading mode, by default 'w'
        config_fn: str, Path, optional
            Filename of model config file, by default "sfincs.inp"
        write_gis: bool
            Write model files additionally to geotiff and geojson, by default True
        data_libs: List, str
            List of data catalog yaml files, by default None

        """
        # model folders
        self._write_gis = write_gis
        if write_gis and "gis" not in self._FOLDERS:
            self._FOLDERS.append("gis")

        super().__init__(
            root=root,
            mode=mode,
            config_fn=config_fn,
            data_libs=data_libs,
            logger=logger,
        )

        # placeholder grid classes
        self.grid_type = None
        self.reggrid = None
        self.quadtree = None
        self.subgrid = xr.Dataset()

    @property
    def mask(self) -> xr.DataArray | None:
        """Returns model mask"""
        if self.grid_type == "regular":
            if "msk" in self.grid:
                return self.grid["msk"]
            elif self.reggrid is not None:
                return self.reggrid.empty_mask

    @property
    def region(self) -> gpd.GeoDataFrame:
        """Returns the geometry of the active model cells."""
        # NOTE overwrites property in GridModel
        region = gpd.GeoDataFrame()
        if "region" in self.geoms:
            region = self.geoms["region"]
        elif "msk" in self.grid and np.any(self.grid["msk"] > 0):
            da = xr.where(self.mask > 0, 1, 0).astype(np.int16)
            da.raster.set_nodata(0)
            region = da.raster.vectorize().dissolve()
        elif self.reggrid is not None:
            region = self.reggrid.empty_mask.raster.box
        return region

    @property
    def crs(self) -> CRS | None:
        """Returns the model crs"""
        if self.grid_type == "regular":
            return self.reggrid.crs
        elif self.grid_type == "quadtree":
            return self.quadtree.crs

    def set_crs(self, crs: Any) -> None:
        """Sets the model crs"""
        if self.grid_type == "regular":
            self.reggrid.crs = CRS.from_user_input(crs)
            self.grid.raster.set_crs(self.reggrid.crs)
        elif self.grid_type == "quadtree":
            self.quadtree.crs = CRS.from_user_input(crs)

    def setup_grid(
        self,
        x0: float,
        y0: float,
        dx: float,
        dy: float,
        nmax: int,
        mmax: int,
        rotation: float,
        epsg: int,
    ):
        """Setup a regular or quadtree grid.

        Parameters
        ----------
        x0, y0 : float
            x,y coordinates of the origin of the grid
        dx, dy : float
            grid cell size in x and y direction
        mmax, nmax : int
            number of grid cells in x and y direction
        rotation : float, optional
            rotation of grid [degree angle], by default None
        epsg : int, optional
            epsg-code of the coordinate reference system, by default None
        """
        # TODO gdf_refinement for quadtree

        self.config.update(
            x0=x0,
            y0=y0,
            dx=dx,
            dy=dy,
            nmax=nmax,
            mmax=mmax,
            rotation=rotation,
            epsg=epsg,
        )
        self.update_grid_from_config()

    def setup_grid_from_region(
        self,
        region: dict,
        res: float = 100,
        crs: Union[str, int] = "utm",
        rotated: bool = False,
        hydrography_fn: str = None,
        basin_index_fn: str = None,
        dec_origin: int = 0,
        dec_rotation: int = 3,
    ):
        """Setup a regular or quadtree grid from a region.

        Parameters
        ----------
        region : dict
            Dictionary describing region of interest, e.g.:

            * {'bbox': [xmin, ymin, xmax, ymax]}
            * {'geom': 'path/to/polygon_geometry'}

            For a complete overview of all region options,
            see :py:function:~hydromt.workflows.basin_mask.parse_region
        res : float, optional
            grid resolution, by default 100 m
        crs : Union[str, int], optional
            coordinate reference system of the grid
            if "utm" (default) the best UTM zone is selected
            else a pyproj crs string or epsg code (int) can be provided
        grid_type : str, optional
            grid type, "regular" (default) or "quadtree"
        rotated : bool, optional
            if True, a minimum rotated rectangular grid is fitted around the region, by default False
        hydrography_fn : str
            Name of data source for hydrography data.
        basin_index_fn : str
            Name of data source with basin (bounding box) geometries associated with
            the 'basins' layer of `hydrography_fn`. Only required if the `region` is
            based on a (sub)(inter)basins without a 'bounds' argument.
        dec_origin : int, optional
            number of decimals to round the origin coordinates, by default 0
        dec_rotation : int, optional
            number of decimals to round the rotation angle, by default 3

        See Also
        --------
        hydromt.workflows.basin_mask.parse_region
        """
        # setup `region` of interest of the model.
        self.setup_region(
            region=region,
            hydrography_fn=hydrography_fn,
            basin_index_fn=basin_index_fn,
        )
        # get pyproj crs of best UTM zone if crs=utm
        pyproj_crs = hydromt.gis_utils.parse_crs(
            crs, self.region.to_crs(4326).total_bounds
        )
        if self.geoms["region"].crs != pyproj_crs:
            self.geoms["region"] = self.geoms["region"].to_crs(pyproj_crs)

        # create grid from region
        # NOTE keyword rotated is added to still have the possibility to create unrotated grids if needed (e.g. for FEWS?)
        if rotated:
            geom = self.geoms["region"].unary_union
            x0, y0, mmax, nmax, rot = utils.rotated_grid(
                geom, res, dec_origin=dec_origin, dec_rotation=dec_rotation
            )
        else:
            x0, y0, x1, y1 = self.geoms["region"].total_bounds
            x0, y0 = round(x0, dec_origin), round(y0, dec_origin)
            mmax = int(np.ceil((x1 - x0) / res))
            nmax = int(np.ceil((y1 - y0) / res))
            rot = 0
        self.setup_grid(
            x0=x0,
            y0=y0,
            dx=res,
            dy=res,
            nmax=nmax,
            mmax=mmax,
            rotation=rot,
            epsg=pyproj_crs.to_epsg(),
        )

    def setup_dep(
        self,
        datasets_dep: List[dict],
        buffer_cells: int = 0,  # not in list
        interp_method: str = "linear",  # used for buffer cells only
    ):
        """Interpolate topobathy (dep) data to the model grid.

        Adds model grid layers:

        * **dep**: combined elevation/bathymetry [m+ref]

        Parameters
        ----------
        datasets_dep : List[dict]
            List of dictionaries with topobathy data, each containing a dataset name or Path (elevtn) and optional merge arguments e.g.:
            [{'elevtn': merit_hydro, 'zmin': 0.01}, {'elevtn': gebco, 'offset': 0, 'merge_method': 'first', reproj_method: 'bilinear'}]
            For a complete overview of all merge options, see :py:function:~hydromt.workflows.merge_multi_dataarrays
        buffer_cells : int, optional
            Number of cells between datasets to ensure smooth transition of bed levels, by default 0
        interp_method : str, optional
            Interpolation method used to fill the buffer cells , by default "linear"
        """

        # retrieve model resolution to determine zoom level for xyz-datasets
        # TODO fix for quadtree
        if not self.mask.raster.crs.is_geographic:
            res = np.abs(self.mask.raster.res[0])
        else:
            res = np.abs(self.mask.raster.res[0]) * 111111.0

        datasets_dep = self._parse_datasets_dep(datasets_dep, res=res)

        if self.grid_type == "regular":
            da_dep = workflows.merge_multi_dataarrays(
                da_list=datasets_dep,
                da_like=self.mask,
                buffer_cells=buffer_cells,
                interp_method=interp_method,
                logger=self.logger,
            )

            # check if no nan data is present in the bed levels
            if not np.isnan(da_dep).any():
                self.logger.warning(
                    f"Interpolate data at {int(np.sum(np.isnan(da_dep.values)))} cells"
                )
                da_dep = da_dep.raster.interpolate_na(method="rio_idw")

            self.set_grid(da_dep, name="dep")
            # FIXME this shouldn't be necessary, since da_dep should already have the crs
            if self.crs is not None and self.grid.raster.crs is None:
                self.grid.set_crs(self.crs)

            if "depfile" not in self.config:
                self.config.update({"depfile": "sfincs.dep"})
        elif self.grid_type == "quadtree":
            raise NotImplementedError(
                "Create dep not yet implemented for quadtree grids."
            )

    def setup_mask_active(
        self,
        mask: Union[str, Path, gpd.GeoDataFrame] = None,
        include_mask: Union[str, Path, gpd.GeoDataFrame] = None,
        exclude_mask: Union[str, Path, gpd.GeoDataFrame] = None,
        mask_buffer: int = 0,
        zmin: float = None,
        zmax: float = None,
        fill_area: float = 10.0,
        drop_area: float = 0.0,
        connectivity: int = 8,
        all_touched: bool = True,
        reset_mask: bool = False,
    ):
        """Setup active model cells.

        The SFINCS model mask defines inactive (msk=0), active (msk=1), and waterlevel boundary (msk=2)
        and outflow boundary (msk=3) cells. This method sets the active and inactive cells.

        Active model cells are based on a region and cells with valid elevation (i.e. not nodata),
        optionally bounded by areas inside the include geomtries, outside the exclude geomtries,
        larger or equal than a minimum elevation threshhold and smaller or equal than a
        maximum elevation threshhold.
        All conditions are combined using a logical AND operation.

        Sets model layers:

        * **msk** map: model mask [-]

        Parameters
        ----------
        mask: str, Path, gpd.GeoDataFrame, optional
            Path or data source name of polygons to initiliaze active mask with; proceding arguments can be used to include/exclude cells
            If not given, existing mask (if present) used, else mask is initialized empty.
        include_mask, exclude_mask: str, Path, gpd.GeoDataFrame, optional
            Path or data source name of polygons to include/exclude from the active model domain.
            Note that include (second last) and exclude (last) areas are processed after other critera,
            i.e. `zmin`, `zmax` and `drop_area`, and thus overrule these criteria for active model cells.
        mask_buffer: float, optional
            If larger than zero, extend the `include_mask` geometry with a buffer [m],
            by default 0.
        zmin, zmax : float, optional
            Minimum and maximum elevation thresholds for active model cells.
        fill_area : float, optional
            Maximum area [km2] of contiguous cells below `zmin` or above `zmax` but surrounded
            by cells within the valid elevation range to be kept as active cells, by default 10 km2.
        drop_area : float, optional
            Maximum area [km2] of contiguous cells to be set as inactive cells, by default 0 km2.
        connectivity, {4, 8}:
            The connectivity used to define contiguous cells, if 4 only horizontal and vertical
            connections are used, if 8 (default) also diagonal connections.
        all_touched: bool, optional
            if True (default) include (or exclude) a cell in the mask if it touches any of the
            include (or exclude) geometries. If False, include a cell only if its center is
            within one of the shapes, or if it is selected by Bresenham's line algorithm.
        reset_mask: bool, optional
            If True, reset existing mask layer. If False (default) updating existing mask.
        """
        # read geometries
        gdf_mask, gdf_include, gdf_exclude = None, None, None
        bbox = self.region.to_crs(4326).total_bounds
        if mask is not None:
            if not isinstance(mask, gpd.GeoDataFrame) and str(mask).endswith(".pol"):
                # NOTE polygons should be in same CRS as model
                gdf_mask = utils.polygon2gdf(
                    feats=utils.read_geoms(fn=mask), crs=self.region.crs
                )
            else:
                gdf_mask = self.data_catalog.get_geodataframe(mask, bbox=bbox)
            if mask_buffer > 0:  # NOTE assumes model in projected CRS!
                gdf_mask["geometry"] = gdf_mask.to_crs(self.crs).buffer(mask_buffer)
        if include_mask is not None:
            if not isinstance(include_mask, gpd.GeoDataFrame) and str(
                include_mask
            ).endswith(".pol"):
                # NOTE polygons should be in same CRS as model
                gdf_include = utils.polygon2gdf(
                    feats=utils.read_geoms(fn=include_mask), crs=self.region.crs
                )
            else:
                gdf_include = self.data_catalog.get_geodataframe(
                    include_mask, bbox=bbox
                )
        if exclude_mask is not None:
            if not isinstance(exclude_mask, gpd.GeoDataFrame) and str(
                exclude_mask
            ).endswith(".pol"):
                gdf_exclude = utils.polygon2gdf(
                    feats=utils.read_geoms(fn=exclude_mask), crs=self.region.crs
                )
            else:
                gdf_exclude = self.data_catalog.get_geodataframe(
                    exclude_mask, bbox=bbox
                )

        # get mask
        if self.grid_type == "regular":
            da_mask = self.reggrid.create_mask_active(
                da_mask=self.grid["msk"] if "msk" in self.grid else None,
                da_dep=self.grid["dep"] if "dep" in self.grid else None,
                gdf_mask=gdf_mask,
                gdf_include=gdf_include,
                gdf_exclude=gdf_exclude,
                zmin=zmin,
                zmax=zmax,
                fill_area=fill_area,
                drop_area=drop_area,
                connectivity=connectivity,
                all_touched=all_touched,
                reset_mask=reset_mask,
                logger=self.logger,
            )
            self.set_grid(da_mask, name="msk")
            # update config
            if "mskfile" not in self.config:
                self.config.update({"mskfile": "sfincs.msk"})
            if "indexfile" not in self.config:
                self.config.update({"indexfile": "sfincs.ind"})
            # update region
            self.logger.info("Derive region geometry based on active cells.")
            region = da_mask.where(da_mask <= 1, 1).raster.vectorize()
            self.set_geoms(region, "region")

    def setup_mask_bounds(
        self,
        btype: str = "waterlevel",
        include_mask: Union[str, Path, gpd.GeoDataFrame] = None,
        exclude_mask: Union[str, Path, gpd.GeoDataFrame] = None,
        zmin: float = None,
        zmax: float = None,
        connectivity: int = 8,
        all_touched: bool = False,
        reset_bounds: bool = False,
    ):
        """Set boundary cells in the model mask.

        The SFINCS model mask defines inactive (msk=0), active (msk=1), and waterlevel boundary (msk=2)
        and outflow boundary (msk=3) cells. Active cells set using the `setup_mask` method,
        while this method sets both types of boundary cells, see `btype` argument.

        Boundary cells at the edge of the active model domain,
        optionally bounded by areas inside the include geomtries, outside the exclude geomtries,
        larger or equal than a minimum elevation threshhold and smaller or equal than a
        maximum elevation threshhold.
        All conditions are combined using a logical AND operation.

        Updates model layers:

        * **msk** map: model mask [-]

        Parameters
        ----------
        btype: {'waterlevel', 'outflow'}
            Boundary type
        include_mask, exclude_mask: str, Path, gpd.GeoDataFrame, optional
            Path or data source name for geometries with areas to include/exclude from the model boundary.
        zmin, zmax : float, optional
            Minimum and maximum elevation thresholds for boundary cells.
            Note that when include and exclude areas are used, the elevation range is only applied
            on cells within the include area and outside the exclude area.
        reset_bounds: bool, optional
            If True, reset existing boundary cells of the selected boundary
            type (`btype`) before setting new boundary cells, by default False.
        all_touched: bool, optional
            if True (default) include (or exclude) a cell in the mask if it touches any of the
            include (or exclude) geometries. If False, include a cell only if its center is
            within one of the shapes, or if it is selected by Bresenham's line algorithm.
        connectivity, {4, 8}:
            The connectivity used to detect the model edge, if 4 only horizontal and vertical
            connections are used, if 8 (default) also diagonal connections.
        """

        # get include / exclude geometries
        gdf_include, gdf_exclude = None, None
        bbox = self.region.to_crs(4326).total_bounds
        if include_mask is not None:
            if not isinstance(include_mask, gpd.GeoDataFrame) and str(
                include_mask
            ).endswith(".pol"):
                # NOTE polygons should be in same CRS as model
                gdf_include = utils.polygon2gdf(
                    feats=utils.read_geoms(fn=include_mask), crs=self.region.crs
                )
            else:
                gdf_include = self.data_catalog.get_geodataframe(
                    include_mask, bbox=bbox
                )
        if exclude_mask is not None:
            if not isinstance(exclude_mask, gpd.GeoDataFrame) and str(
                exclude_mask
            ).endswith(".pol"):
                gdf_exclude = utils.polygon2gdf(
                    feats=utils.read_geoms(fn=exclude_mask), crs=self.region.crs
                )
            else:
                gdf_exclude = self.data_catalog.get_geodataframe(
                    exclude_mask, bbox=bbox
                )

        # mask values
        if self.grid_type == "regular":
            da_mask = self.reggrid.create_mask_bounds(
                da_mask=self.grid["msk"],
                btype=btype,
                gdf_include=gdf_include,
                gdf_exclude=gdf_exclude,
                da_dep=self.grid["dep"] if "dep" in self.grid else None,
                zmin=zmin,
                zmax=zmax,
                connectivity=connectivity,
                all_touched=all_touched,
                reset_bounds=reset_bounds,
                logger=self.logger,
            )
            self.set_grid(da_mask, name="msk")

    def setup_subgrid(
        self,
        datasets_dep: List[dict],
        datasets_rgh: List[dict] = [],
        buffer_cells: int = 0,
        nbins: int = 10,
        nr_subgrid_pixels: int = 20,
        nrmax: int = 2000,  # blocksize
        max_gradient: float = 5.0,
        z_minimum: float = -99999.0,
        manning_land: float = 0.04,
        manning_sea: float = 0.02,
        rgh_lev_land: float = 0.0,
        write_dep_tif: bool = False,
        write_man_tif: bool = False,
    ):
        """Setup method for subgrid tables based on a list of
        elevation and Manning's roughness datasets.

        These datasets are used to derive relations between the water level
        and the volume in a cell to do the continuity update,
        and a representative water depth used to calculate momentum fluxes.

        This allows that one can compute on a coarser computational grid,
        while still accounting for the local topography and roughness.

        Parameters
        ----------
        datasets_dep : List[dict]
            List of dictionaries with topobathy data.
            Each should minimally contain a data catalog source name, data file path, or xarray raster object ('elevtn')
            Optional merge arguments include 'zmin', 'zmax', 'mask', 'offset', 'reproj_method', and 'merge_method'.
            e.g.: [{'elevtn': merit_hydro, 'zmin': 0.01}, {'elevtn': gebco, 'offset': 0, 'merge_method': 'first', reproj_method: 'bilinear'}]
            For a complete overview of all merge options, see :py:function:~hydromt.workflows.merge_multi_dataarrays
        datasets_rgh : List[dict], optional
            List of dictionaries with Manning's n datasets. Each dictionary should at least contain one of the following:
            * (1) manning: filename (or Path) of gridded data with manning values
            * (2) lulc (and reclass_table) :a combination of a filename of gridded landuse/landcover and a mapping table.
            In additon, optional merge arguments can be provided e.g.: merge_method, gdf_valid_fn
        buffer_cells : int, optional
            Number of cells between datasets to ensure smooth transition of bed levels, by default 0
        nbins : int, optional
            Number of bins in the subgrid tables, by default 10
        nr_subgrid_pixels : int, optional
            Number of subgrid pixels per computational cell, by default 20
        nrmax : int, optional
            Maximum number of cells per subgrid-block, by default 2000
            These blocks are used to prevent memory issues while working with large datasets
        max_gradient : float, optional
            Maximum gradient in the subgrid tables, by default 5.0
        z_minimum : float, optional
            Minimum depth in the subgrid tables, by default -99999.0
        manning_land, manning_sea : float, optional
            Constant manning roughness values for land and sea, by default 0.04 and 0.02 s.m-1/3
            Note that these values are only used when no Manning's n datasets are provided, or to fill the nodata values
        rgh_lev_land : float, optional
            Elevation level to distinguish land and sea roughness (when using manning_land and manning_sea), by default 0.0
        write_dep_tif : bool, optional
            Create geotiff of the merged topobathy on the subgrid resolution, by default False
        write_man_tif : bool, optional
            Create geotiff of the merged roughness on the subgrid resolution, by default False
        """

        # retrieve model resolution
        # TODO fix for quadtree
        if not self.mask.raster.crs.is_geographic:
            res = np.abs(self.mask.raster.res[0]) / nr_subgrid_pixels
        else:
            res = np.abs(self.mask.raster.res[0]) * 111111.0 / nr_subgrid_pixels

        datasets_dep = self._parse_datasets_dep(datasets_dep, res=res)

        if len(datasets_rgh) > 0:
            # NOTE conversion from landuse/landcover to manning happens here
            datasets_rgh = self._parse_datasets_rgh(datasets_rgh)

        # folder where high-resolution topobathy and manning geotiffs are stored
        if write_dep_tif or write_man_tif:
            highres_dir = os.path.join(self.root, "subgrid")
            if not os.path.isdir(highres_dir):
                os.makedirs(highres_dir)
        else:
            highres_dir = None

        if self.grid_type == "regular":
            self.reggrid.subgrid.build(
                da_mask=self.mask,
                datasets_dep=datasets_dep,
                datasets_rgh=datasets_rgh,
                buffer_cells=buffer_cells,
                nbins=nbins,
                nr_subgrid_pixels=nr_subgrid_pixels,
                nrmax=nrmax,
                max_gradient=max_gradient,
                z_minimum=z_minimum,
                manning_land=manning_land,
                manning_sea=manning_sea,
                rgh_lev_land=rgh_lev_land,
                write_dep_tif=write_dep_tif,
                write_man_tif=write_man_tif,
                highres_dir=highres_dir,
                logger=self.logger,
            )
            self.subgrid = self.reggrid.subgrid.to_xarray(
                dims=self.mask.raster.dims, coords=self.mask.raster.coords
            )
        elif self.grid_type == "quadtree":
            pass

        if "sbgfile" not in self.config:  # only add sbgfile if not already present
            self.config.update({"sbgfile": "sfincs.sbg"})
        # subgrid is used so no depfile or manningfile needed
        if "depfile" in self.config:
            self.config.pop("depfile")  # remove depfile from config
        if "manningfile" in self.config:
            self.config.pop("manningfile")  # remove manningfile from config

    def setup_river_inflow(
        self,
        rivers: Union[str, Path, gpd.GeoDataFrame] = None,
        hydrography: Union[str, Path, xr.Dataset] = None,
        river_upa: float = 10.0,
        river_len: float = 1e3,
        river_width: float = 500,
        merge: bool = False,
        first_index: int = 1,
        keep_rivers_geom: bool = False,
    ):
        """Setup discharge (src) points where a river enters the model domain.

        If `rivers` is not provided, river centerlines are extracted from the
        `hydrography` dataset based on the `river_upa` threshold.

        Waterlevel or outflow boundary cells intersecting with the river
        are removed from the model mask.

        Discharge is set to zero at these points, but can be updated
        using the `setup_discharge_forcing` or `setup_discharge_forcing_from_grid` methods.

        Note: this method assumes the rivers are directed from up- to downstream.

        Adds model layers:

        * **dis** forcing: discharge forcing
        * **mask** map: SFINCS mask layer (only if `river_width` > 0)
        * **rivers_inflow** geoms: river centerline (if `keep_rivers_geom`; not used by SFINCS)

        Parameters
        ----------
        rivers : str, Path, gpd.GeoDataFrame, optional
            Path, data source name, or geopandas object for river centerline data.
            If present, the 'uparea' and 'rivlen' attributes are used.
        hydrography: str, Path, xr.Dataset optional
            Path, data source name, or a xarray raster object for hydrography data.

            * Required layers: ['uparea', 'flwdir'].
        river_upa : float, optional
            Minimum upstream area threshold for rivers [km2], by default 10.0
        river_len: float, optional
            Mimimum river length within the model domain threshhold [m], by default 1 km.
        river_width: float, optional
            Estimated constant width [m] of the inflowing river. Boundary cells within
            half the width are forced to be closed (mask = 1) to avoid instabilities with
            nearby open or waterlevel boundary cells, by default 500 m.
        merge: bool, optional
            If True, merge rivers source points with existing points, by default False.
        first_index: int, optional
            First index for the river source points, by default 1.
        keep_rivers_geom: bool, optional
            If True, keep a geometry of the rivers "rivers_inflow" in geoms. By default False.
        buffer: int, optional
            Buffer [no. of cells] around model domain, by default 10.

        See Also
        --------
        setup_discharge_forcing
        setup_discharge_forcing_from_grid
        """
        da_flwdir, da_uparea, gdf_riv = None, None, None
        if hydrography is not None:
            ds = self.data_catalog.get_rasterdataset(
                hydrography,
                geom=self.region,
                variables=["uparea", "flwdir"],
                buffer=5,
            )
            da_flwdir = ds["flwdir"]
            da_uparea = ds["uparea"]
        elif rivers == "rivers_outflow" and rivers in self.geoms:
            # reuse rivers from setup_river_in/outflow
            gdf_riv = self.geoms[rivers]
        elif rivers is not None:
            gdf_riv = self.data_catalog.get_geodataframe(
                rivers, geom=self.region
            ).to_crs(self.crs)
        else:
            raise ValueError("Either hydrography or rivers must be provided.")

        gdf_src, gdf_riv = workflows.river_boundary_points(
            region=self.region,
            res=self.reggrid.dx,
            gdf_riv=gdf_riv,
            da_flwdir=da_flwdir,
            da_uparea=da_uparea,
            river_len=river_len,
            river_upa=river_upa,
            inflow=True,
        )
        n = len(gdf_src.index)
        self.logger.info(f"Found {n} river inflow points.")
        if n == 0:
            return

        # set forcing src pnts
        gdf_src.index = gdf_src.index + first_index
        self.set_forcing_1d(gdf_locs=gdf_src.copy(), name="dis", merge=merge)

        # set river
        if keep_rivers_geom:
            self.set_geoms(gdf_riv, name="rivers_inflow")

        # update mask if river_width > 0
        if "rivwth" in gdf_src.columns:
            river_width = gdf_src["rivwth"].fillna(river_width)
        if np.any(river_width > 0) and np.any(self.mask > 1):
            # apply buffer
            gdf_src["geometry"] = gdf_src.buffer(river_width / 2)
            # find intersect of buffer and model grid
            tmp_msk = self.reggrid.create_mask_bounds(
                xr.where(self.mask > 0, 1, 0).astype(np.uint8), gdf_include=gdf_src
            )
            reset_msk = np.logical_and(tmp_msk > 1, self.mask > 1)
            # update model mask
            n = int(np.sum(reset_msk))
            if n > 0:
                da_mask = self.mask.where(~reset_msk, np.uint8(1))
                self.set_grid(da_mask, "msk")
                self.logger.info(f"Boundary cells (n={n}) updated around src points.")

    def setup_river_outflow(
        self,
        rivers: Union[str, Path, gpd.GeoDataFrame] = None,
        hydrography: Union[str, Path, xr.Dataset] = None,
        river_upa: float = 10.0,
        river_len: float = 1e3,
        river_width: float = 500,
        keep_rivers_geom: bool = False,
        reset_bounds: bool = False,
        btype: str = "outflow",
    ):
        """Setup open boundary cells (mask=3) where a river flows
        out of the model domain.

        If `rivers` is not provided, river centerlines are extracted from the
        `hydrography` dataset based on the `river_upa` threshold.

        River outflows that intersect with discharge source point or waterlevel
        boundary cells are omitted.

        Note: this method assumes the rivers are directed from up- to downstream.

        Adds / edits model layers:

        * **msk** map: edited by adding outflow points (msk=3)
        * **rivers_outflow** geoms: river centerline (if `keep_rivers_geom`; not used by SFINCS)

        Parameters
        ----------
        rivers : str, Path, gpd.GeoDataFrame, optional
            Path, data source name, or geopandas object for river centerline data.
            If present, the 'uparea' and 'rivlen' attributes are used.
        hydrography: str, Path, xr.Dataset optional
            Path, data source name, or a xarray raster object for hydrography data.

            * Required layers: ['uparea', 'flwdir'].
        river_upa : float, optional
            Minimum upstream area threshold for rivers [km2], by default 10.0
        river_len: float, optional
            Mimimum river length within the model domain threshhold [m], by default 1000 m.
        river_width: int, optional
            The width [m] of the open boundary cells in the SFINCS msk file.
            By default 500m, i.e.: 250m to each side of the outflow location.
        append_bounds: bool, optional
            If True, write new outflow boundary cells on top of existing. If False (default),
            first reset existing outflow boundary cells to normal active cells.
        keep_rivers_geom: bool, optional
            If True, keep a geometry of the rivers "rivers_outflow" in geoms. By default False.
        reset_bounds: bool, optional
            If True, reset existing outlfow boundary cells before setting new boundary cells,
            by default False.
        btype: {'waterlevel', 'outflow'}
            Boundary type

        See Also
        --------
        setup_mask_bounds
        """
        da_flwdir, da_uparea, gdf_riv = None, None, None
        if hydrography is not None:
            ds = self.data_catalog.get_rasterdataset(
                hydrography,
                geom=self.region,
                variables=["uparea", "flwdir"],
                buffer=5,
            )
            da_flwdir = ds["flwdir"]
            da_uparea = ds["uparea"]
        elif rivers == "rivers_inflow" and rivers in self.geoms:
            # reuse rivers from setup_river_in/outflow
            gdf_riv = self.geoms[rivers]
        elif rivers is not None:
            gdf_riv = self.data_catalog.get_geodataframe(
                rivers, geom=self.region
            ).to_crs(self.crs)
        else:
            raise ValueError("Either hydrography or rivers must be provided.")

        # TODO reproject region and gdf_riv to utm zone if model crs is geographic
        gdf_out, gdf_riv = workflows.river_boundary_points(
            region=self.region,
            res=self.reggrid.dx,
            gdf_riv=gdf_riv,
            da_flwdir=da_flwdir,
            da_uparea=da_uparea,
            river_len=river_len,
            river_upa=river_upa,
            inflow=False,
        )

        if len(gdf_out) > 0:
            if "rivwth" in gdf_out.columns:
                river_width = gdf_out["rivwth"].fillna(river_width)
            gdf_out["geometry"] = gdf_out.buffer(river_width / 2)
            # remove points near waterlevel boundary cells
            if np.any(self.mask == 2) and btype == "outflow":
                gdf_msk2 = utils.get_bounds_vector(self.mask)
                # NOTE: this should be a single geom
                geom = gdf_msk2[gdf_msk2["value"] == 2].unary_union
                gdf_out = gdf_out[~gdf_out.intersects(geom)]
            # remove outflow points near source points
            if "dis" in self.forcing and len(gdf_out) > 0:
                geom = self.forcing["dis"].vector.to_gdf().unary_union
                gdf_out = gdf_out[~gdf_out.intersects(geom)]

        # update mask
        n = len(gdf_out.index)
        self.logger.info(f"Found {n} valid river outflow points.")
        if n > 0:
            self.setup_mask_bounds(
                btype=btype, include_mask=gdf_out, reset_bounds=reset_bounds
            )
        elif reset_bounds:
            self.setup_mask_bounds(btype=btype, reset_bounds=reset_bounds)

        # keep river centerlines
        if keep_rivers_geom and len(gdf_riv) > 0:
            self.set_geoms(gdf_riv, name="rivers_outflow")

    def setup_constant_infiltration(self, qinf, reproj_method="average"):
        """Setup spatially varying constant infiltration rate (qinffile).

        Adds model layers:

        * **qinf** map: constant infiltration rate [mm/hr]

        Parameters
        ----------
        qinf : str, Path, or RasterDataset
            Spatially varying infiltration rates [mm/hr]
        reproj_method : str, optional
            Resampling method for reprojecting the infiltration data to the model grid.
            By default 'average'. For more information see, :py:meth:`hydromt.raster.RasterDataArray.reproject_like`
        """

        # get infiltration data
        da_inf = self.data_catalog.get_rasterdataset(qinf, geom=self.region, buffer=10)
        da_inf = da_inf.raster.mask_nodata()  # set nodata to nan

        # reproject infiltration data to model grid
        da_inf = da_inf.raster.reproject_like(self.mask, method=reproj_method)

        # check on nan values
        if np.logical_and(np.isnan(da_inf), self.mask >= 1).any():
            self.logger.warning("NaN values found in infiltration data; filled with 0")
            da_inf = da_inf.fillna(0)
        da_inf.raster.set_nodata(-9999.0)

        # set grid
        mname = "qinf"
        da_inf.attrs.update(**self._ATTRS.get(mname, {}))
        self.set_grid(da_inf, name=mname)

        # update config: remove default inf and set qinf map
        self.set_config(f"{mname}file", f"sfincs.{mname}")
        self.config.pop("qinf", None)

    def setup_cn_infiltration(self, cn, antecedent_moisture="avg", reproj_method="med"):
        """Setup model potential maximum soil moisture retention map (scsfile)
        from gridded curve number map.

        Adds model layers:

        * **scs** map: potential maximum soil moisture retention [inch]

        Parameters
        ---------
        cn: str, Path, or RasterDataset
            Name of gridded curve number map.

            * Required layers without antecedent runoff conditions: ['cn']
            * Required layers with antecedent runoff conditions: ['cn_dry', 'cn_avg', 'cn_wet']
        antecedent_moisture: {'dry', 'avg', 'wet'}, optional
            Antecedent runoff conditions.
            None if data has no antecedent runoff conditions.
            By default `avg`
        reproj_method : str, optional
            Resampling method for reprojecting the curve number data to the model grid.
            By default 'med'. For more information see, :py:meth:`hydromt.raster.RasterDataArray.reproject_like`
        """
        # get data
        da_org = self.data_catalog.get_rasterdataset(cn, geom=self.region, buffer=10)
        # read variable
        v = "cn"
        if antecedent_moisture:
            v = f"cn_{antecedent_moisture}"
        if isinstance(da_org, xr.Dataset) and v in da_org.data_vars:
            da_org = da_org[v]
        elif not isinstance(da_org, xr.DataArray):
            raise ValueError(f"Could not find variable {v} in {cn}")

        # reproject using median
        da_cn = da_org.raster.reproject_like(self.grid, method=reproj_method)

        # convert to potential maximum soil moisture retention S (1000/CN - 10) [inch]
        da_scs = workflows.cn_to_s(da_cn, self.mask > 0).round(3)

        # set grid
        mname = "scs"
        da_scs.attrs.update(**self._ATTRS.get(mname, {}))
        self.set_grid(da_scs, name=mname)
        # update config: remove default infiltration values and set scs map
        self.config.pop("qinf", None)
        self.set_config(f"{mname}file", f"sfincs.{mname}")

    def setup_manning_roughness(
        self,
        datasets_rgh: List[dict] = [],
        manning_land=0.04,
        manning_sea=0.02,
        rgh_lev_land=0,
    ):
        """Setup model manning roughness map (manningfile) from gridded manning data or a combinataion of gridded
        land-use/land-cover map and manning roughness mapping table.

        Adds model layers:

        * **man** map: manning roughness coefficient [s.m-1/3]

        Parameters
        ---------
        datasets_rgh : List[dict], optional
            List of dictionaries with Manning's n datasets. Each dictionary should at least contain one of the following:
            * (1) manning: filename (or Path) of gridded data with manning values
            * (2) lulc (and reclass_table) :a combination of a filename of gridded landuse/landcover and a mapping table.
            In additon, optional merge arguments can be provided e.g.: merge_method, gdf_valid_fn
        manning_land, manning_sea : float, optional
            Constant manning roughness values for land and sea, by default 0.04 and 0.02 s.m-1/3
            Note that these values are only used when no Manning's n datasets are provided, or to fill the nodata values
        rgh_lev_land : float, optional
            Elevation level to distinguish land and sea roughness (when using manning_land and manning_sea), by default 0.0
        """

        if len(datasets_rgh) > 0:
            datasets_rgh = self._parse_datasets_rgh(datasets_rgh)
        else:
            datasets_rgh = []

        # fromdep keeps track of whether any manning values should be based on the depth or not
        fromdep = len(datasets_rgh) == 0
        if self.grid_type == "regular":
            if len(datasets_rgh) > 0:
                da_man = workflows.merge_multi_dataarrays(
                    da_list=datasets_rgh,
                    da_like=self.mask,
                    interp_method="linear",
                    logger=self.logger,
                )
                fromdep = np.isnan(da_man).where(self.mask > 0, False).any()
            if "dep" in self.grid and fromdep:
                da_man0 = xr.where(
                    self.grid["dep"] >= rgh_lev_land, manning_land, manning_sea
                )
            elif fromdep:
                da_man0 = xr.full_like(self.mask, manning_land, dtype=np.float32)

            if len(datasets_rgh) > 0 and fromdep:
                self.logger.warning("nan values in manning roughness array")
                da_man = da_man.where(~np.isnan(da_man), da_man0)
            elif fromdep:
                da_man = da_man0
            da_man.raster.set_nodata(-9999.0)

            # set grid
            mname = "manning"
            da_man.attrs.update(**self._ATTRS.get(mname, {}))
            self.set_grid(da_man, name=mname)
            # update config: remove default manning values and set maning map
            for v in ["manning_land", "manning_sea", "rgh_lev_land"]:
                self.config.pop(v, None)
            self.set_config(f"{mname}file", f"sfincs.{mname[:3]}")

    def setup_observation_points(
        self,
        locations: Union[str, Path, gpd.GeoDataFrame],
        merge: bool = True,
        **kwargs,
    ):
        """Setup model observation point locations.

        Adds model layers:

        * **obs** geom: observation point locations

        Parameters
        ---------
        locations: str, Path, gpd.GeoDataFrame, optional
            Path, data source name, or geopandas object for observation point locations.
        merge: bool, optional
            If True, merge the new observation points with the existing ones. By default True.
        """
        name = self._GEOMS["observation_points"]

        # FIXME ensure the catalog is loaded before adding any new entries
        self.data_catalog.sources

        gdf_obs = self.data_catalog.get_geodataframe(
            locations, geom=self.region, assert_gtype="Point", **kwargs
        ).to_crs(self.crs)

        if not gdf_obs.geometry.type.isin(["Point"]).all():
            raise ValueError("Observation points must be of type Point.")

        if merge and name in self.geoms:
            gdf0 = self._geoms.pop(name)
            gdf_obs = gpd.GeoDataFrame(pd.concat([gdf_obs, gdf0], ignore_index=True))
            self.logger.info(f"Adding new observation points to existing ones.")

        self.set_geoms(gdf_obs, name)
        self.set_config(f"{name}file", f"sfincs.{name}")

    def setup_structures(
        self,
        structures: Union[str, Path, gpd.GeoDataFrame],
        stype: str,
        dz: float = None,
        merge: bool = True,
        **kwargs,
    ):
        """Setup thin dam or weir structures.

        Adds model layer (depending on `stype`):

        * **thd** geom: thin dam
        * **weir** geom: weir / levee

        Parameters
        ----------
        structures : str, Path
            Path, data source name, or geopandas object to structure line geometry file.
            The "name" (for thd and weir), "z" and "par1" (for weir only) variables are optional.
            For weirs: `dz` must be provided if gdf has no "z" column or ZLineString;
            "par1" defaults to 0.6 if gdf has no "par1" column.
        stype : {'thd', 'weir'}
            Structure type.
        merge : bool, optional
            If True, merge with existing'stype' structures, by default True.
        dz: float, optional
            If provided, for weir structures the z value is calculated from
            the model elevation (dep) plus dz.
        """

        # read, clip and reproject
        gdf_structures = self.data_catalog.get_geodataframe(
            structures, geom=self.region, **kwargs
        ).to_crs(self.crs)

        cols = {
            "thd": ["name", "geometry"],
            "weir": ["name", "z", "par1", "geometry"],
        }
        assert stype in cols
        gdf = gdf_structures[
            [c for c in cols[stype] if c in gdf_structures.columns]
        ]  # keep relevant cols

        structs = utils.gdf2linestring(gdf)  # check if it parsed correct
        # sample zb values from dep file and set z = zb + dz
        if stype == "weir" and dz is not None:
            elv = self.grid["dep"]
            structs_out = []
            for s in structs:
                pnts = gpd.points_from_xy(x=s["x"], y=s["y"])
                zb = elv.raster.sample(gpd.GeoDataFrame(geometry=pnts, crs=self.crs))
                s["z"] = zb.values + float(dz)
                structs_out.append(s)
            gdf = utils.linestring2gdf(structs_out, crs=self.crs)
        # Else function if you define elevation of weir
        elif stype == "weir" and np.any(["z" not in s for s in structs]):
            raise ValueError("Weir structure requires z values.")
        # combine with existing structures if present
        if merge and stype in self.geoms:
            gdf0 = self._geoms.pop(stype)
            gdf = gpd.GeoDataFrame(pd.concat([gdf, gdf0], ignore_index=True))
            self.logger.info(f"Adding {stype} structures to existing structures.")

        # set structures
        self.set_geoms(gdf, stype)
        self.set_config(f"{stype}file", f"sfincs.{stype}")

    def setup_drainage_structures(
        self,
        structures: Union[str, Path, gpd.GeoDataFrame],
        stype: str = "pump",
        discharge: float = 0.0,
        merge: bool = True,
        **kwargs,
    ):
        """Setup drainage structures.

        Adds model layer:
        * **drn** geom: drainage pump or culvert

        Parameters
        ----------
        structures : str, Path
            Path, data source name, or geopandas object to structure line (with 2 points per line!) geometry file.
            The "type" (1 for pump and 2 for culvert), "par1" ("discharge" also accepted) variables are optional.
            If "type" or "par1" are not provided, they are based on stype or discharge arguments.
        stype : {'pump', 'culvert'}, optional
            Structure type, by default "pump". stype is converted to integer "type" to match with SFINCS expectations.
        discharge : float, optional
            Discharge of the structure, by default 0.0. For culverts, this is the maximum discharge,
            since actual discharge depends on waterlevel gradient
        merge : bool, optional
            If True, merge with existing drainage structures, by default True.
        """

        stype = stype.lower()
        svalues = {"pump": 1, "culvert": 2}
        if stype not in svalues:
            raise ValueError('stype must be one of "pump", "culvert"')
        svalue = svalues[stype]

        # read, clip and reproject
        gdf_structures = self.data_catalog.get_geodataframe(
            structures, geom=self.region, **kwargs
        ).to_crs(self.crs)

        # check if type (int) is present in gdf, else overwrite from args
        # TODO also add check if type is interger?
        if "type" not in gdf_structures:
            gdf_structures["type"] = svalue
        # if discharge is provided, rename to par1
        if "discharge" in gdf_structures:
            gdf_structures["par1"] = gdf_structures["discharge"]
            gdf_structures = gdf_structures.drop(columns=["discharge"])

        # add par1, par2, par3, par4, par5 if not present
        # NOTE only par1 is used in the model
        if "par1" not in gdf_structures:
            gdf_structures["par1"] = discharge
        if "par2" not in gdf_structures:
            gdf_structures["par2"] = 0
        if "par3" not in gdf_structures:
            gdf_structures["par3"] = 0
        if "par4" not in gdf_structures:
            gdf_structures["par4"] = 0
        if "par5" not in gdf_structures:
            gdf_structures["par5"] = 0

        # check if (multi)linestrings only have 2 points, else drop
        for i, row in gdf_structures.iterrows():
            if isinstance(row.geometry, MultiLineString):
                for ls in row.geometry.geoms:
                    if len(ls.coords) != 2:
                        self.logger.debug(
                            f"Row {i} contains a MultiLineString with a LineString that has {len(ls.coords)} points."
                        )
                        gdf_structures = gdf_structures.drop(index=i)
                    else:
                        # convert Multilinestring to LineString
                        gdf_structures.loc[i, "geometry"] = LineString(ls.coords)
            elif isinstance(row.geometry, LineString) and len(row.geometry.coords) != 2:
                self.logger.debug(
                    f"Row {i} contains a LineString with {len(row.geometry.coords)} points."
                )
                gdf_structures = gdf_structures.drop(index=i)

        # combine with existing structures if present
        if merge and "drn" in self.geoms:
            gdf0 = self._geoms.pop("drn")
            gdf_structures = gpd.GeoDataFrame(
                pd.concat([gdf_structures, gdf0], ignore_index=True)
            )
            self.logger.info(f"Adding {stype} structures to existing structures.")

        # set structures
        self.set_geoms(gdf_structures, "drn")
        self.set_config("drnfile", f"sfincs.drn")

    ### FORCING
    def set_forcing_1d(
        self,
        df_ts: pd.DataFrame = None,
        gdf_locs: gpd.GeoDataFrame = None,
        name: str = "bzs",
        merge: bool = True,
    ):
        """Set 1D forcing time series for 'bzs' or 'dis' boundary conditions.

        1D forcing exists of point location `gdf_locs` and associated timeseries `df_ts`.
        If `gdf_locs` is None, the currently set locations are used.

        If merge is True, time series in `df_ts` with the same index will
        overwrite existing data. Time series with new indices are added to
        the existing forcing.

        In case the forcing time series have a numeric index, the index is converted to
        a datetime index assuming the index is in seconds since `tref`.

        Parameters
        ----------
        df_ts : pd.DataFrame, optional
            1D forcing time series data. If None, dummy forcing data is added.
        gdf_locs : gpd.GeoDataFrame, optional
            Location of waterlevel boundary points. If None, the currently set locations are used.
        name : str, optional
            Name of the waterlevel boundary time series file, by default 'bzs'.
        merge : bool, optional
            If True, merge with existing forcing data, by default True.
        """
        # check dtypes
        if gdf_locs is not None:
            if not isinstance(gdf_locs, gpd.GeoDataFrame):
                raise ValueError("gdf_locs must be a gpd.GeoDataFrame")
            if not gdf_locs.index.is_integer() and gdf_locs.index.is_unique:
                raise ValueError("gdf_locs index must be unique integer values")
            if not gdf_locs.geometry.type.isin(["Point"]).all():
                raise ValueError("gdf_locs geometry must be Point")
            if gdf_locs.crs != self.crs:
                gdf_locs = gdf_locs.to_crs(self.crs)
        elif name in self.forcing:
            gdf_locs = self.forcing[name].vector.to_gdf()
        if df_ts is not None:
            if not isinstance(df_ts, pd.DataFrame):
                raise ValueError("df_ts must be a pd.DataFrame")
            if not df_ts.columns.is_integer() and df_ts.columns.is_unique:
                raise ValueError("df_ts column names must be unique integer values")
        # parse datetime index
        if df_ts is not None and df_ts.index.is_numeric():
            if "tref" not in self.config:
                raise ValueError(
                    "tref must be set in config to convert numeric index to datetime index"
                )
            tref = utils.parse_datetime(self.config["tref"])
            df_ts.index = tref + pd.to_timedelta(df_ts.index, unit="sec")
        # parse location index
        if (
            gdf_locs is not None
            and df_ts is not None
            and gdf_locs.index.size == df_ts.columns.size
            and not set(gdf_locs.index) == set(df_ts.columns)
        ):
            # loop over integer columns and find matching index
            for col in gdf_locs.select_dtypes(include=np.integer).columns:
                if set(gdf_locs[col]) == set(df_ts.columns):
                    gdf_locs = gdf_locs.set_index(col)
                    self.logger.info(f"Setting gdf_locs index to {col}")
                    break
            if not (gdf_locs.index) == set(df_ts.columns):
                gdf_locs = gdf_locs.set_index(df_ts.columns)
                self.logger.info(
                    f"No matching index column found in gdf_locs; assuming the order is correct"
                )
        # merge with existing data
        if name in self.forcing and merge:
            # read existing data
            da = self.forcing[name]
            gdf0 = da.vector.to_gdf()
            df0 = da.transpose(..., da.vector.index_dim).to_pandas()
            if set(gdf0.index) != set(gdf_locs.index):
                # merge locations; overwrite existing locations with the same name
                gdf0 = gdf0.drop(gdf_locs.index, errors="ignore")
                gdf_locs = pd.concat([gdf0, gdf_locs], axis=0).sort_index()
                # gdf_locs = gpd.GeoDataFrame(gdf_locs, crs=gdf0.crs)
                df0 = df0.reindex(gdf_locs.index, axis=1, fill_value=0)
            if df_ts is None:
                df_ts = df0
            elif set(df0.columns) != set(df_ts.columns):
                # merge timeseries; overwrite existing timeseries with the same name
                df0 = df0.drop(columns=df_ts.columns, errors="ignore")
                df_ts = pd.concat([df0, df_ts], axis=1).sort_index()
                # use linear interpolation and backfill to fill in missing values
                df_ts = df_ts.sort_index()
                df_ts = df_ts.interpolate(method="linear").bfill().fillna(0)
        # location data is required
        if gdf_locs is None:
            raise ValueError(
                f"gdf_locs must be provided if not merged with existing {name} forcing data"
            )
        # fill in missing timeseries
        if df_ts is None:
            df_ts = pd.DataFrame(
                index=pd.date_range(*self.get_model_time(), periods=2),
                data=0,
                columns=gdf_locs.index,
            )
        # set forcing with consistent names
        if not set(gdf_locs.index) == set(df_ts.columns):
            raise ValueError("The gdf_locs index and df_ts columns must be the same")
        gdf_locs.index.name = "index"
        df_ts.columns.name = "index"
        df_ts.index.name = "time"
        da = GeoDataArray.from_gdf(gdf_locs.to_crs(self.crs), data=df_ts, name=name)
        self.set_forcing(da.transpose("time", "index"))

    def setup_waterlevel_forcing(
        self,
        geodataset: Union[str, Path, xr.Dataset] = None,
        timeseries: Union[str, Path, pd.DataFrame] = None,
        locations: Union[str, Path, gpd.GeoDataFrame] = None,
        offset: Union[str, Path, xr.Dataset] = None,
        buffer: float = 5e3,
        merge: bool = True,
    ):
        """Setup waterlevel forcing.

        Waterlevel boundary conditions are read from a `geodataset` (geospatial point timeseries)
        or a tabular `timeseries` dataframe. At least one of these must be provided.

        The tabular timeseries data is combined with `locations` if provided,
        or with existing 'bnd' locations if previously set.

        Adds model forcing layers:

        * **bzs** forcing: waterlevel time series [m+ref]

        Parameters
        ----------
        geodataset: str, Path, xr.Dataset, optional
            Path, data source name, or xarray data object for geospatial point timeseries.
        timeseries: str, Path, pd.DataFrame, optional
            Path, data source name, or pandas data object for tabular timeseries.
        locations: str, Path, gpd.GeoDataFrame, optional
            Path, data source name, or geopandas object for bnd point locations.
        offset: str, Path, xr.Dataset, float, optional
            Path, data source name, constant value or xarray raster data for gridded offset
            between vertical reference of elevation and waterlevel data,
            The offset is added to the waterlevel data.
        buffer: float, optional
            Buffer [m] around model water level boundary cells to select waterlevel gauges,
            by default 5 km.
        merge : bool, optional
            If True, merge with existing forcing data, by default True.

        See Also
        --------
        set_forcing_1d
        """
        gdf_locs, df_ts = None, None
        tstart, tstop = self.get_model_time()  # model time
        # buffer around msk==2 values
        if np.any(self.mask == 2):
            region = self.mask.where(self.mask == 2, 0).raster.vectorize()
        else:
            region = self.region
        # read waterlevel data from geodataset or geodataframe
        if geodataset is not None:
            # read and clip data in time & space
            da = self.data_catalog.get_geodataset(
                geodataset,
                geom=region,
                buffer=buffer,
                variables=["waterlevel"],
                time_tuple=(tstart, tstop),
                crs=self.crs,
            )
            df_ts = da.transpose(..., da.vector.index_dim).to_pandas()
            gdf_locs = da.vector.to_gdf()
        elif timeseries is not None:
            df_ts = self.data_catalog.get_dataframe(
                timeseries,
                time_tuple=(tstart, tstop),
                # kwargs below only applied if timeseries not in data catalog
                parse_dates=True,
                index_col=0,
            )
            df_ts.columns = df_ts.columns.map(int)  # parse column names to integers
        else:
            raise ValueError("Either geodataset or timeseries must be provided")

        # optionally read location data (if not already read from geodataset)
        if gdf_locs is None and locations is not None:
            gdf_locs = self.data_catalog.get_geodataframe(
                locations, geom=region, buffer=buffer, crs=self.crs
            ).to_crs(self.crs)
        elif gdf_locs is None and "bzs" in self.forcing:
            gdf_locs = self.forcing["bzs"].vector.to_gdf()
        elif gdf_locs is None:
            raise ValueError("No waterlevel boundary (bnd) points provided.")

        # optionally read offset data and correct df_ts
        if offset is not None and gdf_locs is not None:
            if isinstance(offset, (float, int)):
                df_ts += offset
            else:
                da_offset = self.data_catalog.get_rasterdataset(
                    offset, geom=self.region, buffer=5
                )
                offset_pnts = da_offset.raster.sample(gdf_locs)
                df_offset = offset_pnts.to_pandas().reindex(df_ts.columns).fillna(0)
                df_ts = df_ts + df_offset
                offset = offset_pnts.mean().values
            self.logger.debug(
                f"waterlevel forcing: applied offset (avg: {offset:+.2f})"
            )

        # set/ update forcing
        self.set_forcing_1d(df_ts, gdf_locs, name="bzs", merge=merge)

    def setup_waterlevel_bnd_from_mask(
        self,
        distance: float = 1e4,
        merge: bool = True,
    ):
        """Setup waterlevel boundary (bnd) points along model waterlevel boundary (msk=2).

        The waterlevel boundary (msk=2) should be set before calling this method,
        e.g.: with `setup_mask_bounds`

        Waterlevels (bzs) are set to zero at these points, but can be updated
        with `setup_waterlevel_forcing`.

        Parameters
        ----------
        distance: float, optional
            Distance [m] between waterlevel boundary points,
            by default 10 km.
        merge : bool, optional
            If True, merge with existing forcing data, by default True.

        See Also
        --------
        setup_waterlevel_forcing
        setup_mask_bounds
        """
        # get waterlevel boundary vector based on mask
        gdf_msk = utils.get_bounds_vector(self.mask)
        gdf_msk2 = gdf_msk[gdf_msk["value"] == 2]

        # create points along boundary
        points = []
        for _, row in gdf_msk2.iterrows():
            distances = np.arange(0, row.geometry.length, distance)
            for d in distances:
                point = row.geometry.interpolate(d)
                points.append((point.x, point.y))

        # create geodataframe with points
        gdf = gpd.GeoDataFrame(geometry=gpd.points_from_xy(*zip(*points)), crs=self.crs)

        # set waterlevel boundary
        self.set_forcing_1d(gdf_locs=gdf, name="bzs", merge=merge)

    def setup_discharge_forcing(
        self, geodataset=None, timeseries=None, locations=None, merge=True
    ):
        """Setup discharge forcing.

        Discharge timeseries are read from a `geodataset` (geospatial point timeseries)
        or a tabular `timeseries` dataframe. At least one of these must be provided.

        The tabular timeseries data is combined with `locations` if provided,
        or with existing 'src' locations if previously set, e.g., with the
        `setup_river_inflow` method.

        Adds model layers:

        * **dis** forcing: discharge time series [m3/s]

        Parameters
        ----------
        geodataset: str, Path, xr.Dataset, optional
            Path, data source name, or xarray data object for geospatial point timeseries.
        timeseries: str, Path, pd.DataFrame, optional
            Path, data source name, or pandas data object for tabular timeseries.
        locations: str, Path, gpd.GeoDataFrame, optional
            Path, data source name, or geopandas object for bnd point locations.
        merge : bool, optional
            If True, merge with existing forcing data, by default True.

        See Also
        --------
        setup_river_inflow
        """
        gdf_locs, df_ts = None, None
        tstart, tstop = self.get_model_time()  # model time
        # read waterlevel data from geodataset or geodataframe
        if geodataset is not None:
            # read and clip data in time & space
            da = self.data_catalog.get_geodataset(
                geodataset,
                geom=self.region,
                variables=["discharge"],
                time_tuple=(tstart, tstop),
                crs=self.crs,
            )
            df_ts = da.transpose(..., da.vector.index_dim).to_pandas()
            gdf_locs = da.vector.to_gdf()
        elif timeseries is not None:
            df_ts = self.data_catalog.get_dataframe(
                timeseries,
                time_tuple=(tstart, tstop),
                # kwargs below only applied if timeseries not in data catalog
                parse_dates=True,
                index_col=0,
            )
            df_ts.columns = df_ts.columns.map(int)  # parse column names to integers
        else:
            raise ValueError("Either geodataset or timeseries must be provided")

        # optionally read location data (if not already read from geodataset)
        if gdf_locs is None and locations is not None:
            gdf_locs = self.data_catalog.get_geodataframe(
                locations, geom=self.region, crs=self.crs
            ).to_crs(self.crs)
        elif gdf_locs is None and "dis" in self.forcing:
            gdf_locs = self.forcing["dis"].vector.to_gdf()
        elif gdf_locs is None:
            raise ValueError("No discharge boundary (src) points provided.")

        # set/ update forcing
        self.set_forcing_1d(df_ts, gdf_locs, name="dis", merge=merge)

    def setup_discharge_forcing_from_grid(
        self,
        discharge,
        locations=None,
        uparea=None,
        wdw=1,
        rel_error=0.05,
        abs_error=50,
    ):
        """Setup discharge forcing based on a gridded discharge dataset.

        Discharge boundary timesereis are read from the `discharge` dataset
        with gridded discharge time series data.

        The `locations` are snapped to the `uparea` grid if provided based their
        uparea attribute. If not provided, the nearest grid cell is used.

        Adds model layers:

        * **dis** forcing: discharge time series [m3/s]

        Adds meta layer (not used by SFINCS):

        * **src_snapped** geom: snapped gauge location on discharge grid

        Parameters
        ----------
        discharge: str, Path, xr.DataArray optional
            Path,  data source name or xarray data object for gridded discharge timeseries dataset.

            * Required variables: ['discharge' (m3/s)]
            * Required coordinates: ['time', 'y', 'x']
        locations: str, Path, gpd.GeoDataFrame, optional
            Path, data source name, or geopandas data object for point location dataset.
            Not required if point location have previously been set, e.g. using the
            :py:meth:`~hydromt_sfincs.SfincsModel.setup_river_inflow` method.

            * Required variables: ['uparea' (km2)]
        uparea: str, Path, optional
            Path, data source name, or xarray data object for upstream area grid.

            * Required variables: ['uparea' (km2)]
        wdw: int, optional
            Window size in number of cells around discharge boundary locations
            to snap to, only used if ``uparea`` is provided. By default 1.
        rel_error, abs_error: float, optional
            Maximum relative error (default 0.05) and absolute error (default 50 km2)
            between the discharge boundary location upstream area and the upstream area of
            the best fit grid cell, only used if "discharge" geoms has a "uparea" column.

        See Also
        --------
        setup_river_inflow
        """
        if locations is not None:
            gdf = self.data_catalog.get_geodataframe(
                locations, geom=self.region, assert_gtype="Point"
            ).to_crs(self.crs)
        elif "dis" in self.forcing:
            gdf = self.forcing["dis"].vector.to_gdf()
        else:
            raise ValueError("No discharge boundary (src) points provided.")

        # read data
        ds = self.data_catalog.get_rasterdataset(
            discharge,
            geom=self.region,
            buffer=2,
            time_tuple=self.get_model_time(),  # model time
            variables=["discharge"],
            single_var_as_array=False,
        )
        if uparea is not None and "uparea" in gdf.columns:
            da_upa = self.data_catalog.get_rasterdataset(
                uparea, geom=self.region, buffer=2, variables=["uparea"]
            )
            # make sure ds and da_upa align
            ds["uparea"] = da_upa.raster.reproject_like(ds, method="nearest")
        elif "uparea" not in gdf.columns:
            self.logger.warning('No "uparea" column found in location data.')

        # TODO use hydromt core method
        ds_snapped = workflows.snap_discharge(
            ds=ds,
            gdf=gdf,
            wdw=wdw,
            rel_error=rel_error,
            abs_error=abs_error,
            uparea_name="uparea",
            discharge_name="discharge",
            logger=self.logger,
        )
        # set zeros for src points without matching discharge
        da_q = ds_snapped["discharge"].reindex(index=gdf.index, fill_value=0).fillna(0)
        df_q = da_q.transpose("time", ...).to_pandas()
        # update forcing
        self.set_forcing_1d(df_ts=df_q, gdf_locs=gdf, name="dis")
        # keep snapped locations
        self.set_geoms(ds_snapped.vector.to_gdf(), "src_snapped")

    def setup_precip_forcing_from_grid(
        self, precip=None, dst_res=None, aggregate=False, **kwargs
    ):
        """Setup precipitation forcing from a gridded spatially varying data source.

        If aggregate is True, spatially uniform precipitation forcing is added to
        the model based on the mean precipitation over the model domain.
        If aggregate is False, distributed precipitation is added to the model as netcdf file.
        The data is reprojected to the model CRS (and destination resolution `dst_res` if provided).

        Adds one of these model layer:

        * **netamprfile** forcing: distributed precipitation [mm/hr]
        * **precipfile** forcing: uniform precipitation [mm/hr]

        Parameters
        ----------
        precip, str, Path
            Path to precipitation rasterdataset netcdf file.

            * Required variables: ['precip' (mm)]
            * Required coordinates: ['time', 'y', 'x']

        dst_res: float
            output resolution (m), by default None and computed from source data.
            Only used in combination with aggregate=False
        aggregate: bool, {'mean', 'median'}, optional
            Method to aggregate distributed input precipitation data. If True, mean
            aggregation is used, if False (default) the data is not aggregated and
            spatially distributed precipitation is returned.
        """
        # get data for model domain and config time range
        precip = self.data_catalog.get_rasterdataset(
            precip,
            geom=self.region,
            buffer=2,
            time_tuple=self.get_model_time(),
            variables=["precip"],
        )

        # aggregate or reproject in space
        if aggregate:
            stat = aggregate if isinstance(aggregate, str) else "mean"
            self.logger.debug(f"Aggregate precip using {stat}.")
            zone = self.region.dissolve()  # make sure we have a single (multi)polygon
            precip_out = precip.raster.zonal_stats(zone, stats=stat)[f"precip_{stat}"]
            df_ts = precip_out.where(precip_out >= 0, 0).fillna(0).squeeze().to_pandas()
            self.setup_precip_forcing(df_ts.to_frame())
        else:
            # reproject to model utm crs
            # NOTE: currently SFINCS errors (stack overflow) on large files,
            # downscaling to model grid is not recommended
            kwargs0 = dict(align=dst_res is not None, method="nearest_index")
            kwargs0.update(kwargs)
            meth = kwargs0["method"]
            self.logger.debug(f"Resample precip using {meth}.")
            precip_out = precip.raster.reproject(
                dst_crs=self.crs, dst_res=dst_res, **kwargs
            ).fillna(0)

            # resample in time
            precip_out = hydromt.workflows.resample_time(
                precip_out,
                freq=pd.to_timedelta("1H"),
                conserve_mass=True,
                upsampling="bfill",
                downsampling="sum",
                logger=self.logger,
            ).rename("precip")

            # add to forcing
            self.set_forcing(precip_out, name="precip")

    def setup_precip_forcing(self, timeseries):
        """Setup spatially uniform precipitation forcing (precip).

        Adds model layers:

        * **precipfile** forcing: uniform precipitation [mm/hr]

        Parameters
        ----------
        timeseries, str, Path
            Path to tabulated timeseries csv file with time index in first column
            and location IDs in the first row,
            see :py:meth:`hydromt.open_timeseries_from_table`, for details.
            Note: tabulated timeseries files cannot yet be set through the data_catalog yml file.
        """
        tstart, tstop = self.get_model_time()
        df_ts = self.data_catalog.get_dataframe(
            timeseries,
            time_tuple=(tstart, tstop),
            # kwargs below only applied if timeseries not in data catalog
            parse_dates=True,
            index_col=0,
        )
        if isinstance(df_ts, pd.DataFrame):
            df_ts = df_ts.squeeze()
        if not isinstance(df_ts, pd.Series):
            raise ValueError("df_ts must be a pandas.Series")
        df_ts.name = "precip"
        df_ts.index.name = "time"
        self.set_forcing(df_ts.to_xarray(), name="precip")

    def setup_tiles(
        self,
        path: Union[str, Path] = None,
        region: dict = None,
        datasets_dep: List[dict] = [],
        zoom_range: Union[int, List[int]] = [0, 13],
        z_range: List[int] = [-20000.0, 20000.0],
        create_index_tiles: bool = True,
        create_topobathy_tiles: bool = True,
        fmt: str = "bin",
    ):
        """Create both index and topobathy tiles in webmercator format.

        Parameters
        ----------
        path : Union[str, Path]
            Directory in which to store the index tiles, if None, the model root + tiles is used.
        region : dict
            Dictionary describing region of interest, e.g.:
            * {'bbox': [xmin, ymin, xmax, ymax]}. Note bbox should be provided in WGS 84
            * {'geom': 'path/to/polygon_geometry'}
            If None, the model region is used.
        datasets_dep : List[dict]
            List of dictionaries with topobathy data, each containing a dataset name or Path (elevtn) and optional merge arguments e.g.:
            [{'elevtn': merit_hydro, 'zmin': 0.01}, {'elevtn': gebco, 'offset': 0, 'merge_method': 'first', reproj_method: 'bilinear'}]
            For a complete overview of all merge options, see :py:function:~hydromt.workflows.merge_multi_dataarrays
            Note that subgrid/dep_subgrid.tif is automatically used if present and datasets_dep is left empty.
        zoom_range : Union[int, List[int]], optional
            Range of zoom levels for which tiles are created, by default [0,13]
        z_range : List[int], optional
            Range of valid elevations that are included in the topobathy tiles, by default [-20000.0, 20000.0]
        create_index_tiles : bool, optional
            If True, index tiles are created, by default True
        create_topobathy_tiles : bool, optional
            If True, topobathy tiles are created, by default True.
        fmt : str, optional
            Format of the tiles: "bin" (binary, default), or "png".
        """
        # use model root if path not provided
        if path is None:
            path = os.path.join(self.root, "tiles")

        # use model region if region not provided
        if region is None:
            region = self.region
        else:
            _kind, _region = hydromt.workflows.parse_region(region=region)
            if "bbox" in _region:
                bbox = _region["bbox"]
                region = gpd.GeoDataFrame(geometry=[box(*bbox)], crs=4326)
            elif "geom" in _region:
                region = _region["geom"]
                if region.crs is None:
                    raise ValueError('Model region "geom" has no CRS')

        # if only one zoom level is specified, create tiles up to that zoom level (inclusive)
        if isinstance(zoom_range, int):
            zoom_range = [0, zoom_range]

        # create index tiles
        if create_index_tiles:
            # only binary and png are supported for index tiles so set to binary if tif
            fmt_ind = "bin" if fmt == "tif" else fmt

            if self.grid_type == "regular":
                self.reggrid.create_index_tiles(
                    region=region,
                    root=path,
                    zoom_range=zoom_range,
                    fmt=fmt_ind,
                    logger=self.logger,
                )
            elif self.grid_type == "quadtree":
                raise NotImplementedError(
                    "Index tiles not yet implemented for quadtree grids."
                )

        # create topobathy tiles
        if create_topobathy_tiles:
            # compute resolution of highest zoom level
            # resolution of zoom level 0  on equator: 156543.03392804097
            res = 156543.03392804097 / 2 ** zoom_range[1]
            datasets_dep = self._parse_datasets_dep(datasets_dep, res=res)

            # if no datasets provided, check if high-res subgrid geotiff is there
            if len(datasets_dep) == 0:
                if os.path.exists(os.path.join(self.root, "subgrid")):
                    # check if there is a dep_subgrid.tif
                    dep = os.path.join(self.root, "subgrid", "dep_subgrid.tif")
                    if os.path.exists(dep):
                        da = self.data_catalog.get_rasterdataset(dep)
                        datasets_dep.append({"da": da})
                    else:
                        raise ValueError("No topobathy datasets provided.")

            # create topobathy tiles
            workflows.tiling.create_topobathy_tiles(
                root=path,
                region=region,
                datasets_dep=datasets_dep,
                index_path=os.path.join(path, "index"),
                zoom_range=zoom_range,
                z_range=z_range,
                fmt=fmt,
            )

    # Plotting
    def plot_forcing(self, fn_out=None, **kwargs):
        """Plot model timeseries forcing.

        For distributed forcing a spatial avarage is plotted.

        Parameters
        ----------
        fn_out: str
            Path to output figure file.
            If a basename is given it is saved to <model_root>/figs/<fn_out>
            If None, no file is saved.
        forcing : Dict of xr.DataArray
            Model forcing

        Returns
        -------
        fig, axes
            Model fig and ax objects
        """
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt

        if self.forcing:
            forcing = {}
            for name in self.forcing:
                if isinstance(self.forcing[name], xr.Dataset):
                    continue  # plot only dataarrays
                forcing[name] = self.forcing[name]
                # update missing attributes for plot labels
                forcing[name].attrs.update(**self._ATTRS.get(name, {}))
            if len(forcing) > 0:
                fig, axes = plots.plot_forcing(forcing, **kwargs)
                # set xlim to model tstart - tend
                tstart, tstop = self.get_model_time()
                axes[-1].set_xlim(mdates.date2num([tstart, tstop]))

                # save figure
                if fn_out is not None:
                    if not os.path.isabs(fn_out):
                        fn_out = join(self.root, "figs", fn_out)
                    if not os.path.isdir(dirname(fn_out)):
                        os.makedirs(dirname(fn_out))
                    plt.savefig(fn_out, dpi=225, bbox_inches="tight")
                return fig, axes
        else:
            raise ValueError("No forcing found in model.")

    def plot_basemap(
        self,
        fn_out: str = None,
        variable: str = "dep",
        shaded: bool = False,
        plot_bounds: bool = True,
        plot_region: bool = False,
        plot_geoms: bool = True,
        bmap: str = None,
        zoomlevel: int = 11,
        figsize: Tuple[int] = None,
        geom_names: List[str] = None,
        geom_kwargs: Dict = {},
        legend_kwargs: Dict = {},
        **kwargs,
    ):
        """Create basemap plot.

        Parameters
        ----------
        fn_out: str, optional
            Path to output figure file, by default None.
            If a basename is given it is saved to <model_root>/figs/<fn_out>
            If None, no file is saved.
        variable : str, optional
            Map of variable in ds to plot, by default 'dep'
        shaded : bool, optional
            Add shade to variable (only for variable = 'dep' and non-rotated grids),
            by default False
        plot_bounds : bool, optional
            Add waterlevel (msk=2) and open (msk=3) boundary conditions to plot.
        plot_region : bool, optional
            If True, plot region outline.
        plot_geoms : bool, optional
            If True, plot available geoms.
        bmap : {'sat', 'osm'}, optional
            background map, by default None
        zoomlevel : int, optional
            zoomlevel, by default 11
        figsize : Tuple[int], optional
            figure size, by default None
        geom_names : List[str], optional
            list of model geometries to plot, by default all model geometries.
        geom_kwargs : Dict of Dict, optional
            Model geometry styling per geometry, passed to geopandas.GeoDataFrame.plot method.
            For instance: {'src': {'markersize': 30}}.
        legend_kwargs : Dict, optional
            Legend kwargs, passed to ax.legend method.

        Returns
        -------
        fig, axes
            Model fig and ax objects
        """
        import matplotlib.pyplot as plt

        # combine geoms and forcing locations
        sg = self.geoms.copy()
        for fname, gname in self._FORCING_1D.values():
            if fname[0] in self.forcing and gname is not None:
                try:
                    sg.update({gname: self._forcing[fname[0]].vector.to_gdf()})
                except ValueError:
                    self.logger.debug(f'unable to plot forcing location: "{fname}"')
        if plot_region and "region" not in self.geoms:
            sg.update({"region": self.region})

        # make sure grid are set
        if variable.startswith("subgrid.") and self.subgrid:
            ds = self.subgrid.copy()
            variable = variable.replace("subgrid.", "")
        else:
            ds = self.grid.copy()
        if "msk" not in ds:
            ds["msk"] = self.mask

        fig, ax = plots.plot_basemap(
            ds,
            sg,
            variable=variable,
            shaded=shaded,
            plot_bounds=plot_bounds,
            plot_region=plot_region,
            plot_geoms=plot_geoms,
            bmap=bmap,
            zoomlevel=zoomlevel,
            figsize=figsize,
            geom_names=geom_names,
            geom_kwargs=geom_kwargs,
            legend_kwargs=legend_kwargs,
            **kwargs,
        )

        if fn_out is not None:
            if not os.path.isabs(fn_out):
                fn_out = join(self.root, "figs", fn_out)
            if not os.path.isdir(dirname(fn_out)):
                os.makedirs(dirname(fn_out))
            plt.savefig(fn_out, dpi=225, bbox_inches="tight")

        return fig, ax

    # I/O
    def read(self, epsg: int = None):
        """Read the complete model schematization and configuration from file."""
        self.read_config(epsg=epsg)
        if epsg is None and "epsg" not in self.config:
            raise ValueError(f"Please specify epsg to read this model")
        self.read_grid()
        self.read_subgrid()
        self.read_geoms()
        self.read_forcing()
        self.logger.info("Model read")

    def write(self):
        """Write the complete model schematization and configuration to file."""
        self.logger.info(f"Writing model data to {self.root}")
        # TODO - add check for subgrid & quadtree > give flags to self.write_grid() and self.write_config()
        self.write_grid()
        self.write_subgrid()
        self.write_geoms()
        self.write_forcing()
        self.write_states()
        # config last; might be udpated when writing maps, states or forcing
        self.write_config()
        # write data catalog with used data sources
        # self.write_data_catalog()  # new in hydromt v0.4.4

    def read_grid(self, data_vars: Union[List, str] = None) -> None:
        """Read SFINCS binary grid files and save to `grid` attribute.
        Filenames are taken from the `config` attribute (i.e. input file).

        Parameters
        ----------
        data_vars : Union[List, str], optional
            List of data variables to read, by default None (all)
        """

        da_lst = []
        if data_vars is None:
            data_vars = self._MAPS
        elif isinstance(data_vars, str):
            data_vars = list(data_vars)

        # read index file
        ind_fn = self.get_config("indexfile", fallback="sfincs.ind", abs_path=True)
        if not isfile(ind_fn):
            raise IOError(f".ind path {ind_fn} does not exist")

        dtypes = {"msk": "u1"}
        mvs = {"msk": 0}
        if self.reggrid is not None:
            ind = self.reggrid.read_ind(ind_fn=ind_fn)

            for name in data_vars:
                if f"{name}file" in self.config:
                    fn = self.get_config(
                        f"{name}file", fallback=f"sfincs.{name}", abs_path=True
                    )
                    if not isfile(fn):
                        self.logger.warning(f"{name}file not found at {fn}")
                        continue
                    dtype = dtypes.get(name, "f4")
                    mv = mvs.get(name, -9999.0)
                    da = self.reggrid.read_map(fn, ind, dtype, mv, name=name)
                    da_lst.append(da)
            ds = xr.merge(da_lst)
            epsg = self.config.get("epsg", None)
            if epsg is not None:
                ds.raster.set_crs(epsg)
            self.set_grid(ds)

            # keep some metadata maps from gis directory
            fns = glob.glob(join(self.root, "gis", "*.tif"))
            fns = [
                fn
                for fn in fns
                if basename(fn).split(".")[0] not in self.grid.data_vars
            ]
            if fns:
                ds = hydromt.open_mfraster(fns).load()
                self.set_grid(ds)
                ds.close()

    def write_grid(self, data_vars: Union[List, str] = None):
        """Write SFINCS grid to binary files including map index file.
        Filenames are taken from the `config` attribute (i.e. input file).

        If `write_gis` property is True, all grid variables are written to geotiff
        files in a "gis" subfolder.

        Parameters
        ----------
        data_vars : Union[List, str], optional
            List of data variables to write, by default None (all)
        """
        self._assert_write_mode

        dtypes = {"msk": "u1"}  # default to f4
        if self.reggrid and len(self.grid.data_vars) > 0 and "msk" in self.grid:
            # make sure orientation is S->N
            ds_out = self.grid
            if ds_out.raster.res[1] < 0:
                ds_out = ds_out.raster.flipud()
            mask = ds_out["msk"].values

            self.logger.debug("Write binary map indices based on mask.")
            ind_fn = self.get_config("indexfile", abs_path=True)
            self.reggrid.write_ind(ind_fn=ind_fn, mask=mask)

            if data_vars is None:  # write all maps
                data_vars = [v for v in self._MAPS if v in ds_out]
            elif isinstance(data_vars, str):
                data_vars = list(data_vars)
            self.logger.debug(f"Write binary map files: {data_vars}.")
            for name in data_vars:
                if f"{name}file" not in self.config:
                    self.set_config(f"{name}file", f"sfincs.{name}")
                # do not write depfile if subgrid is used
                if (name == "dep" or name == "manning") and self.subgrid:
                    continue
                self.reggrid.write_map(
                    map_fn=self.get_config(f"{name}file", abs_path=True),
                    data=ds_out[name].values,
                    mask=mask,
                    dtype=dtypes.get(name, "f4"),
                )

        if self._write_gis:
            self.write_raster("grid")

    def read_subgrid(self):
        """Read SFINCS subgrid file and add to `subgrid` attribute.
        Filename is taken from the `config` attribute (i.e. input file)."""

        self._assert_read_mode

        if "sbgfile" in self.config:
            fn = self.get_config("sbgfile", abs_path=True)
            if not isfile(fn):
                self.logger.warning(f"sbgfile not found at {fn}")
                return

            self.reggrid.subgrid.load(file_name=fn, mask=self.mask)
            self.subgrid = self.reggrid.subgrid.to_xarray(
                dims=self.mask.raster.dims, coords=self.mask.raster.coords
            )

    def write_subgrid(self):
        """Write SFINCS subgrid file."""
        self._assert_write_mode

        if self.subgrid:
            if f"sbgfile" not in self.config:
                self.set_config(f"sbgfile", f"sfincs.sbg")
            fn = self.get_config(f"sbgfile", abs_path=True)
            self.reggrid.subgrid.save(file_name=fn, mask=self.mask)

    def read_geoms(self):
        """Read geometry files and save to `geoms` attribute.
        Known geometry files mentioned in the sfincs.inp configuration file are read,
        including: bnd/src/obs xy(n) files, thd/weir structure files and drn drainage structure files.

        If other geojson files are present in a "gis" subfolder folder, those are read as well.
        """
        self._assert_read_mode
        # read _GEOMS model files
        for gname in self._GEOMS.values():
            if f"{gname}file" in self.config:
                fn = self.get_config(f"{gname}file", abs_path=True)
                if fn is None:
                    continue
                elif not isfile(fn):
                    self.logger.warning(f"{gname}file not found at {fn}")
                    continue
                if gname in ["thd", "weir"]:
                    struct = utils.read_geoms(fn)
                    gdf = utils.linestring2gdf(struct, crs=self.crs)
                elif gname == "obs":
                    gdf = utils.read_xyn(fn, crs=self.crs)
                elif gname == "drn":
                    gdf = utils.read_drn(fn, crs=self.crs)
                else:
                    gdf = utils.read_xy(fn, crs=self.crs)
                self.set_geoms(gdf, name=gname)
        # read additional geojson files from gis directory
        for fn in glob.glob(join(self.root, "gis", "*.geojson")):
            name = basename(fn).replace(".geojson", "")
            gnames = [f[1] for f in self._FORCING_1D.values() if f[1] is not None]
            skip = gnames + list(self._GEOMS.values())
            if name in skip:
                continue
            gdf = hydromt.open_vector(fn, crs=self.crs)
            self.set_geoms(gdf, name=name)

    def write_geoms(self, data_vars: Union[List, str] = None):
        """Write geoms to bnd/src/obs xy files and thd/weir structure files.
        Filenames are based on the `config` attribute.

        If `write_gis` property is True, all geoms are written to geojson
        files in a "gis" subfolder.

        Parameters
        ----------
        data_vars : list of str, optional
            List of data variables to write, by default None (all)

        """
        self._assert_write_mode

        if self.geoms:
            dvars = self._GEOMS.values()
            if data_vars is not None:
                dvars = [name for name in data_vars if name in self._GEOMS.values()]
            self.logger.info("Write geom files")
            for gname, gdf in self.geoms.items():
                if gname in dvars:
                    if f"{gname}file" not in self.config:
                        self.set_config(f"{gname}file", f"sfincs.{gname}")
                    fn = self.get_config(f"{gname}file", abs_path=True)
                    if gname in ["thd", "weir"]:
                        struct = utils.gdf2linestring(gdf)
                        utils.write_geoms(fn, struct, stype=gname)
                    elif gname == "obs":
                        utils.write_xyn(fn, gdf, crs=self.crs)
                    elif gname == "drn":
                        utils.write_drn(fn, gdf)
                    else:
                        utils.write_xy(fn, gdf, fmt="%8.2f")

            # NOTE: all geoms are written to geojson files in a "gis" subfolder
            if self._write_gis:
                self.write_vector(variables=["geoms"])

    def read_forcing(self, data_vars: List = None):
        """Read forcing files and save to `forcing` attribute.
        Known forcing files mentioned in the sfincs.inp configuration file are read,
        including: bzs/dis/precip ascii files and the netampr netcdf file.

        Parameters
        ----------
        data_vars : list of str, optional
            List of data variables to read, by default None (all)
        """
        self._assert_read_mode
        if isinstance(data_vars, str):
            data_vars = list(data_vars)

        # 1D
        dvars_1d = self._FORCING_1D
        if data_vars is not None:
            dvars_1d = [name for name in data_vars if name in dvars_1d]
        tref = utils.parse_datetime(self.config["tref"])
        for name in dvars_1d:
            ts_names, xy_name = self._FORCING_1D[name]
            # read time series
            da_lst = []
            for ts_name in ts_names:
                ts_fn = self.get_config(f"{ts_name}file", abs_path=True)
                if ts_fn is None or not isfile(ts_fn):
                    if ts_fn is not None:
                        self.logger.warning(f"{ts_name}file not found at {ts_fn}")
                    continue
                df = utils.read_timeseries(ts_fn, tref)
                df.index.name = "time"
                if xy_name is not None:
                    df.columns.name = "index"
                    da = xr.DataArray(df, dims=("time", "index"), name=ts_name)
                else:  # spatially uniform forcing
                    da = xr.DataArray(df[df.columns[0]], dims=("time"), name=ts_name)
                da_lst.append(da)
            ds = xr.merge(da_lst[:])
            # read xy
            if xy_name is not None:
                xy_fn = self.get_config(f"{xy_name}file", abs_path=True)
                if xy_fn is None or not isfile(xy_fn):
                    if xy_fn is not None:
                        self.logger.warning(f"{xy_name}file not found at {xy_fn}")
                else:
                    gdf = utils.read_xy(xy_fn, crs=self.crs)
                    # read attribute data from gis files
                    gis_fn = join(self.root, "gis", f"{xy_name}.geojson")
                    if isfile(gis_fn):
                        gdf1 = gpd.read_file(gis_fn)
                        if "index" in gdf1.columns:
                            gdf1 = gdf1.set_index("index")
                            gdf.index = gdf1.index.values
                            ds = ds.assign_coords(index=gdf1.index.values)
                        if np.any(gdf1.columns != "geometry"):
                            gdf = gpd.sjoin(gdf, gdf1, how="left")[gdf1.columns]
                    # set locations as coordinates dataset
                    ds = GeoDataset.from_gdf(gdf, ds, index_dim="index")
            # save in self.forcing
            if len(ds) > 1:
                # keep wave forcing together
                self.set_forcing(ds, name=name, split_dataset=False)
            elif len(ds) > 0:
                self.set_forcing(ds, split_dataset=True)

        # 2D NETCDF format
        dvars_2d = self._FORCING_NET
        if data_vars is not None:
            dvars_2d = [name for name in data_vars if name in dvars_2d]
        for name in dvars_2d:
            fname, rename = self._FORCING_NET[name]
            fn = self.get_config(f"{fname}file", abs_path=True)
            if fn is None or not isfile(fn):
                if fn is not None:
                    self.logger.warning(f"{name}file not found at {fn}")
                continue
            elif name in ["netbndbzsbzi", "netsrcdis"]:
                ds = GeoDataset.from_netcdf(fn, crs=self.crs, chunks="auto")
            else:
                ds = xr.open_dataset(fn, chunks="auto")
            rename = {k: v for k, v in rename.items() if k in ds}
            if len(rename) > 0:
                ds = ds.rename(rename).squeeze(drop=True)[list(rename.values())]
                self.set_forcing(ds, split_dataset=True)
            else:
                logger.warning(f"No forcing variables found in {fname}file")

    def write_forcing(self, data_vars: Union[List, str] = None):
        """Write forcing to ascii or netcdf (netampr) files.
        Filenames are based on the `config` attribute.

        Parameters
        ----------
        data_vars : list of str, optional
            List of data variables to write, by default None (all)
        """
        self._assert_write_mode

        if self.forcing:
            self.logger.info("Write forcing files")

            tref = utils.parse_datetime(self.config["tref"])
            # for nc files -> time in minutes since tref
            tref_str = tref.strftime("%Y-%m-%d %H:%M:%S")

            # 1D timeseries + location text files
            dvars_1d = self._FORCING_1D
            if data_vars is not None:
                dvars_1d = [name for name in data_vars if name in self._FORCING_1D]
            for name in dvars_1d:
                ts_names, xy_name = self._FORCING_1D[name]
                if (
                    name in self._FORCING_NET
                    and f"{self._FORCING_NET[name][0]}file" in self.config
                ):
                    continue  # write NC file instead of text files
                # work with wavespectra dataset and bzs/dis dataarray
                if name in self.forcing and isinstance(self.forcing[name], xr.Dataset):
                    ds = self.forcing[name]
                else:
                    ds = self.forcing  # dict
                # write timeseries
                da = None
                for ts_name in ts_names:
                    if ts_name not in ds or ds[ts_name].ndim > 2:
                        continue
                    # parse data to dataframe
                    da = ds[ts_name].transpose("time", ...)
                    df = da.to_pandas()
                    # get filenames from config
                    if f"{ts_name}file" not in self.config:
                        self.set_config(f"{ts_name}file", f"sfincs.{ts_name}")
                    fn = self.get_config(f"{ts_name}file", abs_path=True)
                    # write timeseries
                    utils.write_timeseries(fn, df, tref)
                # write xy
                if xy_name and da is not None:
                    # parse data to geodataframe
                    try:
                        gdf = da.vector.to_gdf()
                    except Exception:
                        raise ValueError(f"Locations missing for {name} forcing")
                    # get filenames from config
                    if f"{xy_name}file" not in self.config:
                        self.set_config(f"{xy_name}file", f"sfincs.{xy_name}")
                    fn_xy = self.get_config(f"{xy_name}file", abs_path=True)
                    # write xy
                    utils.write_xy(fn_xy, gdf, fmt="%8.2f")
                    # write geojson file to gis folder
                    self.write_vector(variables=f"forcing.{ts_names[0]}")

            # netcdf forcing
            encoding = dict(
                time={"units": f"minutes since {tref_str}", "dtype": "float64"}
            )
            dvars_2d = self._FORCING_NET
            if data_vars is not None:
                dvars_2d = [name for name in data_vars if name in self._FORCING_NET]
            for name in dvars_2d:
                if (
                    name in self._FORCING_1D
                    and f"{self._FORCING_1D[name][1]}file" in self.config
                ):
                    continue  # timeseries + xy file already written
                fname, rename = self._FORCING_NET[name]
                # combine variables and rename to output names
                rename = {v: k for k, v in rename.items() if v in self.forcing}
                if len(rename) == 0:
                    continue
                ds = xr.merge([self.forcing[v] for v in rename.keys()]).rename(rename)
                # get filename from config
                if f"{fname}file" not in self.config:
                    self.set_config(f"{fname}file", f"{name}.nc")
                fn = self.get_config(f"{fname}file", abs_path=True)
                # write 1D timeseries
                if fname in ["netbndbzsbzi", "netsrcdis"]:
                    ds.vector.to_xy().to_netcdf(fn, encoding=encoding)
                    # write geojson file to gis folder
                    self.write_vector(variables=f"forcing.{list(rename.keys())[0]}")
                # write 2D gridded timeseries
                else:
                    ds.to_netcdf(fn, encoding=encoding)

    def read_states(self):
        """Read waterlevel state (zsini) from binary file and save to `states` attribute.
        The inifile if mentioned in the sfincs.inp configuration file is read.

        """
        self._assert_read_mode

        # read index file
        ind_fn = self.get_config("indexfile", fallback="sfincs.ind", abs_path=True)
        if not isfile(ind_fn):
            raise IOError(f".ind path {ind_fn} does not exist")

        if self.reggrid is not None:
            ind = self.reggrid.read_ind(ind_fn=ind_fn)
            if "inifile" in self.config:
                fn = self.get_config("inifile", abs_path=True)
                if not isfile(fn):
                    self.logger.warning("inifile not found at {fn}")
                    return
                zsini = self.reggrid.read_map(
                    fn, ind, dtype="f4", mv=-9999.0, name="zsini"
                )

                if self.crs is not None:
                    zsini.raster.set_crs(self.crs)
                self.set_states(zsini, "zsini")

    def write_states(self):
        """Write waterlevel state (zsini) to binary map file.
        The filenames is based on the `config` attribute.
        """
        self._assert_write_mode

        name = "zsini"

        if name not in self.states:
            self.logger.warning(f"{name} not in states, skipping")
            return

        if self.reggrid and "msk" in self.grid:
            # make sure orientation is S->N
            ds_out = self.grid
            if ds_out.raster.res[1] < 0:
                ds_out = ds_out.raster.flipud()
            mask = ds_out["msk"].values

            self.logger.debug("Write binary map indices based on mask.")
            # write index file
            ind_fn = self.get_config("indexfile", abs_path=True)
            self.reggrid.write_ind(ind_fn=ind_fn, mask=mask)

            if f"inifile" not in self.config:
                self.set_config(f"inifile", f"sfincs.{name}")
            fn = self.get_config("inifile", abs_path=True)
            da = self.states[name]
            if da.raster.res[1] < 0:
                da = da.raster.flipud()

            self.logger.debug("Write binary water level state inifile")
            self.reggrid.write_map(
                map_fn=fn,
                data=da.values,
                mask=mask,
                dtype="f4",
            )

        if self._write_gis:
            self.write_raster("states")

    def read_results(
        self,
        chunksize=100,
        drop=["crs", "sfincsgrid"],
        fn_map="sfincs_map.nc",
        fn_his="sfincs_his.nc",
        **kwargs,
    ):
        """Read results from sfincs_map.nc and sfincs_his.nc and save to the `results` attribute.
        The staggered nc file format is translated into hydromt.RasterDataArray formats.
        Additionally, hmax is computed from zsmax and zb if present.

        Parameters
        ----------
        chunksize: int, optional
            chunk size along time dimension, by default 100
        drop: list, optional
            list of variables to drop, by default ["crs", "sfincsgrid"]
        fn_map: str, optional
            filename of sfincs_map.nc, by default "sfincs_map.nc"
        fn_his: str, optional
            filename of sfincs_his.nc, by default "sfincs_his.nc"
        """
        if not isabs(fn_map):
            fn_map = join(self.root, fn_map)
        if isfile(fn_map):
            ds_face, ds_edge = utils.read_sfincs_map_results(
                fn_map,
                ds_like=self.grid,  # TODO: fix for quadtree
                drop=drop,
                logger=self.logger,
                **kwargs,
            )
            # save as dict of DataArray
            self.set_results(ds_face, split_dataset=True)
            self.set_results(ds_edge, split_dataset=True)

        if not isabs(fn_his):
            fn_his = join(self.root, fn_his)
        if isfile(fn_his):
            ds_his = utils.read_sfincs_his_results(
                fn_his, crs=self.crs, chunksize=chunksize
            )
            # drop double vars (map files has priority)
            drop_vars = [v for v in ds_his.data_vars if v in self._results or v in drop]
            ds_his = ds_his.drop_vars(drop_vars)
            self.set_results(ds_his, split_dataset=True)

    def write_raster(
        self,
        variables=["grid", "states", "results.hmax"],
        root=None,
        driver="GTiff",
        compress="deflate",
        **kwargs,
    ):
        """Write model 2D raster variables to geotiff files.

        NOTE: these files are not used by the model by just saved for visualization/
        analysis purposes.

        Parameters
        ----------
        variables: str, list, optional
            Model variables are a combination of attribute and layer (optional) using <attribute>.<layer> syntax.
            Known ratster attributes are ["grid", "states", "results"].
            Different variables can be combined in a list.
            By default, variables is ["grid", "states", "results.hmax"]
        root: Path, str, optional
            The output folder path. If None it defaults to the <model_root>/gis folder (Default)
        kwargs:
            Key-word arguments passed to hydromt.RasterDataset.to_raster(driver='GTiff', compress='lzw').
        """

        # check variables
        if isinstance(variables, str):
            variables = [variables]
        if not isinstance(variables, list):
            raise ValueError(f'"variables" should be a list, not {type(list)}.')
        # check root
        if root is None:
            root = join(self.root, "gis")
        if not os.path.isdir(root):
            os.makedirs(root)
        # save to file
        for var in variables:
            vsplit = var.split(".")
            attr = vsplit[0]
            obj = getattr(self, f"_{attr}")
            if obj is None or len(obj) == 0:
                continue  # empty
            self.logger.info(f"Write raster file(s) for {var} to 'gis' subfolder")
            layers = vsplit[1:] if len(vsplit) >= 2 else list(obj.keys())
            for layer in layers:
                if layer not in obj:
                    self.logger.warning(f"Variable {attr}.{layer} not found: skipping.")
                    continue
                da = obj[layer]
                if len(da.dims) != 2:
                    # try to reduce to 2D by taking maximum over time dimension
                    if "time" in da.dims:
                        da = da.max("time")
                    elif "timemax" in da.dims:
                        da = da.max("timemax")
                    # if still not 2D, skip
                    if len(da.dims) != 2:
                        self.logger.warning(
                            f"Variable {attr}.{layer} has more than 2 dimensions: skipping."
                        )
                        continue
                # only write active cells to gis files
                da = da.raster.clip_geom(self.region, mask=True).raster.mask_nodata()
                if da.raster.res[1] > 0:  # make sure orientation is N->S
                    da = da.raster.flipud()
                da.raster.to_raster(
                    join(root, f"{layer}.tif"),
                    driver=driver,
                    compress=compress,
                    **kwargs,
                )

    def write_vector(
        self,
        variables=["geoms", "forcing.bzs", "forcing.dis"],
        root=None,
        gdf=None,
        **kwargs,
    ):
        """Write model vector (geoms) variables to geojson files.

        NOTE: these files are not used by the model by just saved for visualization/
        analysis purposes.

        Parameters
        ----------
        variables: str, list, optional
            geoms variables. By default all geoms are saved.
        root: Path, str, optional
            The output folder path. If None it defaults to the <model_root>/gis folder (Default)
        kwargs:
            Key-word arguments passed to geopandas.GeoDataFrame.to_file(driver='GeoJSON').
        """
        kwargs.update(driver="GeoJSON")  # fixed
        # check variables
        if isinstance(variables, str):
            variables = [variables]
        if not isinstance(variables, list):
            raise ValueError(f'"variables" should be a list, not {type(list)}.')
        # check root
        if root is None:
            root = join(self.root, "gis")
        if not os.path.isdir(root):
            os.makedirs(root)
        # save to file
        for var in variables:
            vsplit = var.split(".")
            attr = vsplit[0]
            obj = getattr(self, f"_{attr}")
            if obj is None or len(obj) == 0:
                continue  # empty
            self.logger.info(f"Write vector file(s) for {var} to 'gis' subfolder")
            names = vsplit[1:] if len(vsplit) >= 2 else list(obj.keys())
            for name in names:
                if name not in obj:
                    self.logger.warning(f"Variable {attr}.{name} not found: skipping.")
                    continue
                if isinstance(obj[name], gpd.GeoDataFrame):
                    gdf = obj[name]
                else:
                    try:
                        gdf = obj[name].vector.to_gdf()
                        # xy name -> difficult!
                        name = [
                            v[-1] for v in self._FORCING_1D.values() if name in v[0]
                        ][0]
                    except:
                        self.logger.debug(
                            f"Variable {attr}.{name} could not be written to vector file."
                        )
                        pass
                gdf.to_file(join(root, f"{name}.geojson"), **kwargs)

    ## model configuration

    def read_config(self, config_fn: str = "sfincs.inp", epsg: int = None) -> None:
        """Parse config from SFINCS input file.
        If in write-only mode the config is initialized with default settings.

        Parameters
        ----------
        config_fn: str
            Filename of config file, by default "sfincs.inp".
            If in a different folder than the model root, the root is updated.
        epsg: int
            EPSG code of the model CRS. Only used if missing in the SFINCS input file, by default None.
        """
        inp = SfincsInput()  # initialize with defaults
        if self._read:  # in read-only or append mode, try reading config_fn
            if not isfile(config_fn) and not isabs(config_fn) and self._root:
                # path relative to self.root
                config_fn = abspath(join(self.root, config_fn))
            elif isfile(config_fn) and abspath(dirname(config_fn)) != self._root:
                # new root
                mode = (
                    "r+"
                    if self._write and self._read
                    else ("w" if self._write else "r")
                )
                root = abspath(dirname(config_fn))
                self.logger.warning(f"updating the model root to: {root}")
                self.set_root(root=root, mode=mode)
            else:
                raise IOError(f"SFINCS input file not found {config_fn}")
            # read config_fn
            inp.read(inp_fn=config_fn)
        # overwrite / initialize config attribute
        self._config = inp.to_dict()
        if epsg is not None and "epsg" not in self.config:
            self.config.update(epsg=epsg)
        self.update_grid_from_config()  # update grid properties based on sfincs.inp

    def write_config(self, config_fn: str = "sfincs.inp"):
        """Write config to <root/config_fn>"""
        self._assert_write_mode
        if not isabs(config_fn) and self._root:
            config_fn = join(self.root, config_fn)

        inp = SfincsInput.from_dict(self.config)
        inp.write(inp_fn=abspath(config_fn))

    def update_spatial_attrs(self):
        """Update geospatial `config` (sfincs.inp) attributes based on grid"""
        dx, dy = self.res
        # TODO check self.bounds with rotation!! origin not necessary equal to total_bounds
        west, south, _, _ = self.bounds
        if self.crs is not None:
            self.set_config("epsg", self.crs.to_epsg())
        self.set_config("mmax", self.width)
        self.set_config("nmax", self.height)
        self.set_config("dx", dx)
        self.set_config("dy", abs(dy))  # dy is always positive (orientation is S -> N)
        self.set_config("x0", west)
        self.set_config("y0", south)

    def update_grid_from_config(self):
        """Update grid properties based on `config` (sfincs.inp) attributes"""
        self.grid_type = (
            "quadtree" if self.config.get("qtrfile") is not None else "regular"
        )
        if self.grid_type == "regular":
            self.reggrid = RegularGrid(
                x0=self.config.get("x0"),
                y0=self.config.get("y0"),
                dx=self.config.get("dx"),
                dy=self.config.get("dy"),
                nmax=self.config.get("nmax"),
                mmax=self.config.get("mmax"),
                rotation=self.config.get("rotation", 0),
                epsg=self.config.get("epsg"),
            )
        else:
            raise not NotImplementedError("Quadtree grid not implemented yet")
            # self.quadtree = QuadtreeGrid()

    def get_model_time(self):
        """Return (tstart, tstop) tuple with parsed model start and end time"""
        tstart = utils.parse_datetime(self.config["tstart"])
        tstop = utils.parse_datetime(self.config["tstop"])
        return tstart, tstop

    ## helper method

    def _parse_datasets_dep(self, datasets_dep, res):
        """Parse filenames or paths of Datasets in list of dictionaries datasets_dep into xr.DataArray and gdf.GeoDataFrames:
        * "elevtn" is parsed into da (xr.DataArray)
        * "offset" is parsed into da_offset (xr.DataArray)
        * "mask" is parsed into gdf (gpd.GeoDataFrame)

        Parameters
        ----------
        datasets_dep : List[dict]
            List of dictionaries with topobathy data, each containing a dataset name or Path (dep) and optional merge arguments.
        res : float
            Resolution of the model grid in meters. Used to obtain the correct zoom level of the depth datasets.
        """
        parse_keys = ["elevtn", "offset", "mask", "da"]
        copy_keys = ["zmin", "zmax", "reproj_method", "merge_method"]

        datasets_out = []
        for dataset in datasets_dep:
            dd = {}
            # read in depth datasets; replace dep (source name; filename or xr.DataArray)
            if "elevtn" in dataset or "da" in dataset:
                try:
                    da_elv = self.data_catalog.get_rasterdataset(
                        dataset.get("elevtn", dataset.get("da")),
                        geom=self.mask.raster.box,
                        buffer=10,
                        variables=["elevtn"],
                        zoom_level=(res, "meter"),
                    )
                    dd.update({"da": da_elv})
                except:
                    data_name = dataset.get("elevtn")
                    self.logger.warning(
                        f"{data_name} not used; probably because all the data is outside of the mask."
                    )
                    continue
            else:
                raise ValueError(
                    "No 'elevtn' (topobathy) dataset provided in datasets_dep."
                )

            # read offset filenames
            # NOTE offsets can be xr.DataArrays and floats
            if "offset" in dataset and not isinstance(dataset["offset"], (float, int)):
                da_offset = self.data_catalog.get_rasterdataset(
                    dataset.get("offset"),
                    geom=self.mask.raster.box,
                    buffer=20,
                )
                dd.update({"offset": da_offset})

            # read geodataframes describing valid areas
            if "mask" in dataset:
                gdf_valid = self.data_catalog.get_geodataframe(
                    path_or_key=dataset.get("mask"),
                    geom=self.mask.raster.box,
                )
                dd.update({"gdf_valid": gdf_valid})

            # copy remaining keys
            for key, value in dataset.items():
                if key in copy_keys and key not in dd:
                    dd.update({key: value})
                elif key not in copy_keys + parse_keys:
                    self.logger.warning(f"Unknown key {key} in datasets_dep. Ignoring.")
            datasets_out.append(dd)

        return datasets_out

    def _parse_datasets_rgh(self, datasets_rgh):
        """Parse filenames or paths of Datasets in list of dictionaries datasets_rgh into xr.DataArrays and gdf.GeoDataFrames:
        * "manning" is parsed into da (xr.DataArray)
        * "lulc" is parsed into da (xr.DataArray) using reclassify table in "reclass_table"
        * "mask" is parsed into gdf_valid (gpd.GeoDataFrame)

        Parameters
        ----------
        datasets_rgh : List[dict], optional
            List of dictionaries with Manning's n datasets. Each dictionary should at least contain one of the following:
            * (1) manning: filename (or Path) of gridded data with manning values
            * (2) lulc (and reclass_table) :a combination of a filename of gridded landuse/landcover and a reclassify table.
            In additon, optional merge arguments can be provided e.g.: merge_method, mask
        """
        parse_keys = ["manning", "lulc", "reclass_table", "mask", "da"]
        copy_keys = ["reproj_method", "merge_method"]

        datasets_out = []
        for dataset in datasets_rgh:
            dd = {}

            if "manning" in dataset or "da" in dataset:
                da_man = self.data_catalog.get_rasterdataset(
                    dataset.get("manning", dataset.get("da")),
                    geom=self.mask.raster.box,
                    buffer=10,
                )
                dd.update({"da": da_man})
            elif "lulc" in dataset:
                # landuse/landcover should always be combined with mapping
                lulc = dataset.get("lulc")
                reclass_table = dataset.get("reclass_table", None)
                if reclass_table is None and isinstance(lulc, str):
                    reclass_table = join(DATADIR, "lulc", f"{lulc}_mapping.csv")
                if not os.path.isfile(reclass_table) and isinstance(lulc, str):
                    raise IOError(
                        f"Manning roughness mapping file not found: {reclass_table}"
                    )
                da_lulc = self.data_catalog.get_rasterdataset(
                    lulc, geom=self.mask.raster.box, buffer=10, variables=["lulc"]
                )
                df_map = self.data_catalog.get_dataframe(reclass_table, index_col=0)
                # reclassify
                da_man = da_lulc.raster.reclassify(df_map[["N"]])["N"]
                dd.update({"da": da_man})
            else:
                raise ValueError("No 'manning' dataset provided in datasets_rgh.")

            # read geodataframes describing valid areas
            if "mask" in dataset:
                gdf_valid = self.data_catalog.get_geodataframe(
                    path_or_key=dataset.get("mask"),
                    geom=self.mask.raster.box,
                )
                dd.update({"gdf_valid": gdf_valid})

            # copy remaining keys
            for key, value in dataset.items():
                if key in copy_keys and key not in dd:
                    dd.update({key: value})
                elif key not in copy_keys + parse_keys:
                    self.logger.warning(f"Unknown key {key} in datasets_rgh. Ignoring.")
            datasets_out.append(dd)

        return datasets_out
