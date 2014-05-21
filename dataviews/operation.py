"""
ViewOperations manipulate dataviews, typically for the purposes of
visualization. Such operations often apply to SheetViews or
SheetStacks and compose the data together in ways that can be viewed
conveniently, often by creating or manipulating color channels.
"""

from collections import OrderedDict
import colorsys
import numpy as np
import matplotlib
from matplotlib import pyplot as plt

import param
from param import ParamOverrides

from .views import Overlay
from .sheetviews import SheetView, SheetStack, SheetLayer, DataGrid, Contours, SheetOverlay
from .dataviews import DataLayer, DataOverlay, DataStack, Stack, Table, TableStack, Curve
from .sheetviews import GridLayout, CoordinateGrid

from .options import options, GrayNearest, StyleOpts, Cycle

rgb_to_hsv = np.vectorize(colorsys.rgb_to_hsv)
hsv_to_rgb = np.vectorize(colorsys.hsv_to_rgb)



stack_mapping = {SheetLayer:SheetStack,
                 DataLayer:DataStack,
                 Table:TableStack}



class ViewOperation(param.ParameterizedFunction):
    """
    A ViewOperation takes one or more views as inputs and processes
    them, returning arbitrary new view objects as output. Individual
    dataviews may be passed in directly while multiple dataviews must
    be passed in as a Stack of the appropriate type. A ViewOperation
    may be used to implement simple dataview manipulations or perform
    complex analysis.

    Internally, ViewOperations operate on the level of individual
    dataviews, processing each layer on an input Stack independently.
    """

    def _process(self, view):
        """
        Process a single input view and output a list of views. When
        multiple views are returned as a list, they will be returned
        to the user as a GridLayout. If a Stack is passed into a
        ViewOperation, the individual layers are processed
        sequentially.
        """
        raise NotImplementedError

    def get_views(self, view, pattern, view_type=SheetView):
        """
        Helper method that return a list of views with labels ending
        with the given pattern and which have the specified type. This
        may be useful to check is a single view satisfies some
        condition or to extract the appropriate views from an Overlay.
        """
        if isinstance(view, Overlay):
            matches = [v for v in view.data if v.label.endswith(pattern)]
        elif isinstance(view, SheetView):
            matches = [view] if view.label.endswith(pattern) else []

        return [match for match in matches if isinstance(match, view_type)]


    def _get_signature(self, view_lists):
        """
        Given a sequence of view lists generated by the _process
        method, find the fundamental view types required to select the
        appropriate Stack type. All lists are examined to ensure that
        the fundamental types are consistent across all items.
        """
        signature = None
        for views in view_lists:
            view_signature = []
            for v in views:
                if type(v) in [CoordinateGrid, DataGrid]:
                    raise NotImplementedError("CoordinateGrid and DataGrid not supported yet")
                stack_type = [k for k in stack_mapping if issubclass(type(v), k)][0]
                view_signature.append(stack_type)

            if signature is None:
                signature = view_signature

            if signature != view_signature:
                raise Exception('The ViewOperation is returning inconsistent types.')

        return signature


    def __call__(self, view, **params):
        self.p = ParamOverrides(self, params)

        if not isinstance(view, Stack):
            views = self._process(view)
            if len(views) > 1:
                return GridLayout(views)
            else:
                return views[0]
        else:
            mapped_items = [(k, self._process(el)) for k, el in view.items()]
            signature = self._get_signature(el[1] for el in mapped_items)

            stack_types = [stack_mapping[tp] for tp in signature]
            stacks = [stack_tp(dimensions=view.dimensions,
                               metadata=view.metadata) for stack_tp in stack_types]

            for k, views in mapped_items:
                for ind, v in enumerate(views):
                    stacks[ind][k] = v

            if len(stacks) == 1:
                return stacks[0]
            else:
                return GridLayout(stacks)



class StackOperation(param.ParameterizedFunction):
    """
    A StackOperation takes a Stack of Views or Overlays as inputs
    and processes them, returning arbitrary new Stack objects as output.
    """

    def __call__(self, stack, **params):
        self.p = ParamOverrides(self, params)

        if not isinstance(stack, Stack):
            raise Exception('StackOperation can only process Stacks.')

        stacks = self._process(stack)

        if len(stacks) == 1:
            return stacks[0]
        else:
            return GridLayout(stacks)


    def _process(self, view):
        """
        Process a single input Stack and output a list of Stacks. When
        multiple Stacks are returned as a list, they will be returned
        to the user as a GridLayout.
        """
        raise NotImplementedError



class RGBA(ViewOperation):
    """
    Accepts an overlay containing either 3 or 4 layers. The first
    three layers are the R,G, B channels and the last layer (if given)
    is the alpha channel.
    """

    def _process(self, overlay):
        if len(overlay) not in [3, 4]:
            raise Exception("Requires 3 or 4 layers to convert to RGB(A)")
        if not all(isinstance(el, SheetView) for el in overlay.data):
            raise Exception("All layers must be SheetViews to convert"
                            " to RGB(A) format")
        if not all(el.depth == 1 for el in overlay.data):
            raise Exception("All SheetViews must have a depth of one for"
                            " conversion to RGB(A) format")

        arrays = []
        for el in overlay.data:
            if el.data.max() > 1.0 or el.data.min() < 0:
                self.warning("Clipping data into the interval [0, 1]")
                el.data.clip(0,1.0)
            arrays.append(el.data)


        return [SheetView(np.dstack(arrays), overlay.bounds,
                          label='RGBA',
                          roi_bounds=overlay.roi_bounds)]


class AlphaOverlay(ViewOperation):
    """
    Accepts an overlay of a SheetView defined with a cmap and converts
    it to an RGBA SheetView. The alpha channel of the result is
    defined by the second layer of the overlay.
    """

    def _process(self, overlay):
        R,G,B,_ = split(cmap2rgb(overlay[0]))
        return [SheetView(RGBA(R*G*B*overlay[1]).data,
                          overlay.bounds,
                          label='AlphaOverlay')]



class HCS(ViewOperation):
    """
    Hue-Confidence-Strength plot.

    Accepts an overlay containing either 2 or 3 layers. The first two
    layers are hue and confidence and the third layer (if available)
    is the strength channel.
    """

    S_multiplier = param.Number(default=1.0, bounds=(0.0,None), doc="""
        Multiplier for the strength value.""")

    C_multiplier = param.Number(default=1.0, bounds=(0.0,None), doc="""
        Multiplier for the confidence value.""")

    flipSC = param.Boolean(default=False, doc="""
        Whether to flip the strength and confidence channels""")

    def _process(self, overlay):
        hue = overlay[0]
        confidence = overlay[1]

        strength_data = overlay[2].data if (len(overlay) == 3) else np.ones(hue.shape)

        if hue.shape != confidence.shape:
            raise Exception("Cannot combine plots with different shapes")

        (h,s,v)= (hue.N.data.clip(0.0, 1.0),
                  (confidence.data * self.p.C_multiplier).clip(0.0, 1.0),
                  (strength_data * self.p.S_multiplier).clip(0.0, 1.0))

        if self.p.flipSC:
            (h,s,v) = (h,v,s.clip(0,1.0))

        r, g, b = hsv_to_rgb(h, s, v)
        rgb = np.dstack([r,g,b])
        return [SheetView(rgb, hue.bounds, roi_bounds=overlay.roi_bounds,
                          label=hue.label+' HCS')]



class Colorize(ViewOperation):
    """
    Given a SheetOverlay consisting of a grayscale colormap and a
    second Sheetview with some specified colour map, use the second
    layer to colorize the data of the first layer.

    Currently, colorize only support the 'hsv' color map and is just a
    shortcut to the HCS operation using a constant confidence
    value. Arbitrary colorization will be supported in future.
    """

    def _process(self, overlay):

         if len(overlay) != 2 and overlay[0].mode != 'cmap':
             raise Exception("Can only colorize grayscale overlayed with colour map.")
         if [overlay[0].depth, overlay[1].depth ] != [1,1]:
             raise Exception("Depth one layers required.")
         if overlay[0].shape != overlay[1].shape:
             raise Exception("Shapes don't match.")

         # Needs a general approach which works with any color map
         C = SheetView(np.ones(overlay[1].data.shape),
                       bounds=overlay.bounds)
         hcs = HCS(overlay[1] * C * overlay[0].N)

         return [SheetView(hcs.data, hcs.bounds,
                           roi_bounds=hcs.roi_bounds,
                           label= overlay[0].label+' Colorize')]



class cmap2rgb(ViewOperation):
    """
    Convert SheetViews using colormaps to RGBA mode.  The colormap of
    the style is used, if available. Otherwise, the colormap may be
    forced as a parameter.
    """

    cmap = param.String(default=None, allow_None=True, doc="""
          Force the use of a specific color map. Otherwise, the cmap
          property of the applicable style is used.""")

    def _process(self, sheetview):
        if sheetview.depth != 1:
            raise Exception("Can only apply colour maps to SheetViews with depth of 1.")

        style_cmap = options.style(sheetview)[0].get('cmap', None)
        if not any([self.p.cmap, style_cmap]):
            raise Exception("No color map supplied and no cmap in the active style.")

        cmap = matplotlib.cm.get_cmap(style_cmap if self.p.cmap is None else self.p.cmap)
        return [SheetView(cmap(sheetview.data),
                         bounds=sheetview.bounds,
                         cyclic_range=sheetview.cyclic_range,
                         style=sheetview.style,
                         metadata=sheetview.metadata,
                         label = sheetview.label+' RGB')]



class split(ViewOperation):
    """
    Given SheetViews in RGBA mode, return the R,G,B and A channels as
    a GridLayout.
    """
    def _process(self, sheetview):
        if sheetview.mode not in ['rgb','rgba']:
            raise Exception("Can only split SheetViews with a depth of 3 or 4")
        return [SheetView(sheetview.data[:,:,i],
                          bounds=sheetview.bounds,
                          label='RGBA'[i] + ' Channel')
                for i in range(sheetview.depth)]




class contours(ViewOperation):
    """
    Given a SheetView with a single channel, annotate it with contour
    lines for a given set of contour levels.

    The return is a overlay with a Contours layer for each given
    level, overlaid on top of the input SheetView.
    """

    levels = param.NumericTuple(default=(0.5,), doc="""
         A list of scalar values used to specify the contour levels.""")

    def _process(self, sheetview):

        figure_handle = plt.figure()
        (l,b,r,t) = sheetview.bounds.lbrt()
        contour_set = plt.contour(sheetview.data,
                                  extent=(l,r,t,b),
                                  levels=self.p.levels)

        contours = []
        for level, cset in zip(self.p.levels, contour_set.collections):
            paths = cset.get_paths()
            lines = [path.vertices for path in paths]
            contours.append(Contours(lines, sheetview.bounds,
                            metadata={'level': level},
                            label=sheetview.label+' Level'))

        plt.close(figure_handle)

        if len(contours) == 1:
            return [(sheetview * contours[0])]
        else:
            return [sheetview * SheetOverlay(contours, sheetview.bounds)]


class sample(ViewOperation):
    """
    Given a SheetStack or TableStack sample the data at the sample values
    and return the corresponding TableStack.
    """

    samples = param.List(doc="The list of table headings or sheet coordinate tuples to sample.")

    def _process(self, view):
        if not isinstance(view, (SheetLayer, Table)):
            raise Exception('sample_sheet can only sample SheetLayers.')

        if isinstance(view, Table):
            data = OrderedDict((k, v) for k, v in view.data.items() if k in self.p.samples)
            return [Table(data, label=view.label, metadata=view.metadata)]

        sheetviews = self.get_views(view, '')
        if len(sheetviews) != 1:
            raise Exception('Can only sample, Overlays containing a single SheetView')
        sv = sheetviews[0]
        sample_inds = [(s, tuple(sv.sheet2matrixidx(*s))) for s in self.p.samples]
        data = OrderedDict((sample, sv.data[idx]) for sample, idx in sample_inds)
        return [Table(data, label=sv.label, metadata=sv.metadata)]



class curve_collapse(StackOperation):
    """
    Collapse a stack into a set of Curves or Curve Overlays, where each curve
    corresponds to a given sample. Different dimensions can be chosen for the
    x-axis and by specifying group_by dimensions it is possible to group the
    curves into Overlays.
    """

    x_axis = param.String(default=None, allow_None=True, doc="""
        The dimension by label to be plotted along the x-axis.""")

    group_by = param.List(default=[], doc="""
        The list of dimensions (by label) to be displayed together in a
        Curve overlay.""")

    samples = param.List(default=[], doc="""
        The list of table headings or sheet coordinate tuples to sample into
        curves.""")


    def _process(self, stack):
        self.stack_type = type(stack)

        sampled_stack = sample(stack, samples=self.p.samples)
        self._check_table_stack(sampled_stack)

        x_dim = sampled_stack.dim_dict[self.p.x_axis]
        specified_dims = [self.p.x_axis] + self.p.group_by
        specified_dims_set = set(specified_dims)

        if len(specified_dims) != len(specified_dims_set):
            raise Exception('X axis cannot be included in grouped dimensions.')

        # Dimensions of the output stack
        stack_dims = [d for d in sampled_stack._dimensions if d.name not in specified_dims_set]

       # Get x_axis and non-x_axis dimension values
        split_data = self.split_axis(sampled_stack, self.p.x_axis)

        # Everything except x_axis
        output_dims = [d for d in sampled_stack.dimension_labels if d != self.p.x_axis]
        # Overlays as indexed with the x_axis removed
        overlay_inds = [i for i, name in enumerate(output_dims) if name in self.p.group_by]

        cyclic_range = x_dim.range[1] if x_dim.cyclic else None

        return self._generate_curves(sampled_stack, stack_dims, split_data, overlay_inds, cyclic_range)


    def _check_table_stack(self, stack):
        """
        Make sure the table contains homogenous, numeric values.
        """

        sample_types = [int, float] + np.sctypes['float'] + np.sctypes['int']
        if not all(h in stack._type_map.keys() for h in self.p.samples):
            raise Exception("Invalid list of heading samples.")

        for sample in self.p.samples:
            if stack._type_map[sample] is None:
                raise Exception("Cannot sample inhomogenous type %r" % sample)
            if stack._type_map[sample] not in sample_types:
                raise Exception("Cannot sample from type %r" % stack._type_map[sample].__name__)

        if self.p.x_axis is None:
            raise Exception('x_axis %r not found in stack' % self.p.x_axis)


    def _split_keys_by_axis(self, stack, keys, x_axis):
        """
        Select an axis by name, returning the keys along the chosen
        axis and the corresponding shortened tuple keys.
        """
        x_ndim = stack.dim_index(x_axis)
        xvals = [k[x_ndim] for k in keys]
        dim_vals = [k[:x_ndim] + k[x_ndim+1:] for k in keys]
        return list(OrderedDict.fromkeys(xvals)), list(OrderedDict.fromkeys(dim_vals))


    def split_axis(self, stack, x_axis):
        """
        Returns all stored views such that the specified x_axis
        is eliminated from the full set of stack keys (i.e. each tuple
        key has one element removed corresponding to eliminated dimension).

        As the set of reduced keys is a subset of the original data, each
        reduced key must store multiple x_axis values.

        The return value is an OrderedDict with reduced tuples keys and
        OrderedDict x_axis values (views).
        """

        stack._check_key_type = False # Speed optimization

        x_ndim = stack.dim_index(x_axis)
        keys = list(stack._data.keys())
        x_vals, dim_values = self._split_keys_by_axis(stack, keys, x_axis)

        split_data = OrderedDict()

        for k in dim_values:  # The shortened keys
            split_data[k] = OrderedDict()
            for x in x_vals:  # For a given x_axis value...
                              # Generate a candidate expanded key
                expanded_key = k[:x_ndim] + (x,) + k[x_ndim:]
                if expanded_key in keys:  # If the expanded key actually exists...
                    split_data[k][x] = stack[expanded_key]

        stack._check_key_type = True # Re-enable checks
        return split_data


    def _curve_labels(self, x_axis, sample, ylabel):
        """
        Given the x_axis, sample name and ylabel, returns the formatted curve
        label xlabel and ylabel for a curve. Allows changing the curve labels
        in subclasses of stack.
        """
        if self.stack_type == SheetStack:
            title_prefix = "Coord: %s " % str(sample)
            curve_label = " ".join([x_axis.capitalize(), ylabel])
            return title_prefix, curve_label.title(), x_axis.title(), ylabel.title()
        elif self.stack_type == TableStack:
            return ylabel+' ', str(sample).title(), x_axis.title(), str(sample).title()


    def _generate_curves(self, stack, stack_dims, split_data, overlay_inds, cyclic_range):

        dataviews = []
        for sample in self.p.samples:
            dataview = DataStack(dimensions=stack_dims, metadata=stack.metadata,
                                 title=stack.title) if stack_dims else None
            for key, x_axis_data in split_data.items():
                # Key contains all dimensions (including overlaid dimensions) except for x_axis
                sampled_curve_data = [(x, view[sample]) for x, view in x_axis_data.items()]

                # Compute overlay dimensions
                overlay_items = [(name, key[ind]) for name, ind in
                                 zip(self.p.group_by, overlay_inds)]
                # Generate labels
                legend_label = ', '.join(stack.dim_dict[name].pprint_value(val)
                                         for name, val in overlay_items)
                ylabel = list(x_axis_data.values())[0].label
                title_prefix, label, xlabel, ylabel = self._curve_labels(self.p.x_axis,
                                                                         str(sample),
                                                                         ylabel)

                # Generate the curve view
                curve = Curve(sampled_curve_data, cyclic_range=cyclic_range,
                              metadata=stack.metadata, label=label,
                              legend_label=legend_label, xlabel=xlabel,
                              ylabel=ylabel)

                # Return contains no stacks
                if not stack_dims:
                    dataview = curve if dataview is None else dataview * curve
                    continue

                dataview.title = title_prefix + dataview.title

                # Drop overlay dimensions
                stack_key = tuple([kval for ind, kval in enumerate(key)
                                   if ind not in overlay_inds])

                # Create new overlay if necessary, otherwise add to overlay
                if stack_key not in dataview._data.keys():
                    dataview[stack_key] = DataOverlay([curve])
                else:
                    dataview[stack_key] *= curve
            # Completed stack stored for return
            dataviews.append(dataview)
        return dataviews



options.R_Channel = GrayNearest
options.G_Channel = GrayNearest
options.B_Channel = GrayNearest
options.A_Channel = GrayNearest
options.Level_Contours = StyleOpts(color=Cycle(['b', 'g', 'r']))
