# This file is part of Xpra.
# Copyright (C) 2008, 2009 Nathaniel Smith <njs@pobox.com>
# Copyright (C) 2010-2021 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

from xpra.log import Logger
log = Logger("x11", "bindings", "randr")
from xpra.util import envint, csv, iround, first_time, decode_str
from xpra.os_util import strtobytes


MAX_NEW_MODES = envint("XPRA_RANDR_MAX_NEW_MODES", 32)
assert MAX_NEW_MODES>=2


from libc.stdint cimport uintptr_t  #pylint: disable=syntax-error
ctypedef unsigned long CARD32

cdef extern from "X11/X.h":
    unsigned long CurrentTime
    unsigned long Success

######
# Xlib primitives and constants
######
cdef extern from "X11/Xlib.h":
    ctypedef struct Display:
        pass
    # To make it easier to translate stuff in the X header files into
    # appropriate pyrex declarations, without having to untangle the typedefs
    # over and over again, here are some convenience typedefs.  (Yes, CARD32
    # really is 64 bits on 64-bit systems.  Why?  I have no idea.)
    ctypedef CARD32 XID

    ctypedef int Bool
    ctypedef int Status
    ctypedef XID Drawable
    ctypedef XID Window
    ctypedef CARD32 Time

    Window XDefaultRootWindow(Display * display)

###################################
# Randr
###################################
cdef extern from "X11/extensions/randr.h":
    cdef unsigned int RR_Rotate_0
    ctypedef unsigned long XRRModeFlags
    ctypedef unsigned short Connection
    ctypedef unsigned short SubpixelOrder
    ctypedef unsigned short Rotation

cdef extern from "X11/extensions/Xrandr.h":
    ctypedef XID RRMode
    ctypedef XID RROutput
    ctypedef XID RRCrtc

    Bool XRRQueryExtension(Display *, int *, int *)
    Status XRRQueryVersion(Display *, int * major, int * minor)
    ctypedef struct XRRScreenSize:
        int width, height
        int mwidth, mheight
    XRRScreenSize *XRRSizes(Display *dpy, int screen, int *nsizes)
    void XRRSetScreenSize(Display *dpy, Window w, int width, int height, int mmWidth, int mmHeight)
    ctypedef struct XRRModeInfo:
        RRMode              id
        unsigned int        width
        unsigned int        height
        unsigned long       dotClock
        unsigned int        hSyncStart
        unsigned int        hSyncEnd
        unsigned int        hTotal
        unsigned int        hSkew
        unsigned int        vSyncStart
        unsigned int        vSyncEnd
        unsigned int        vTotal
        char                *name
        unsigned int        nameLength
        XRRModeFlags        modeFlags
    ctypedef struct XRRScreenResources:
        Time        timestamp
        Time        configTimestamp
        int         ncrtc
        RRCrtc      *crtcs
        int         noutput
        RROutput    *outputs
        int         nmode
        XRRModeInfo *modes

    ctypedef struct XRROutputInfo:
        Time            timestamp
        RRCrtc          crtc
        char            *name
        int             nameLen
        unsigned long   mm_width
        unsigned long   mm_height
        Connection      connection
        SubpixelOrder   subpixel_order
        int             ncrtc
        RRCrtc          *crtcs
        int             nclone
        RROutput        *clones
        int             nmode
        int             npreferred
        RRMode          *modes

    ctypedef struct XRRCrtcInfo:
        Time            timestamp
        int             x, y
        unsigned int    width, height
        RRMode          mode
        Rotation        rotation
        int             noutput
        RROutput        *outputs
        Rotation        rotations
        int             npossible
        RROutput        *possible

    ctypedef unsigned short SizeID
    ctypedef struct XRRScreenConfiguration:
        pass
    Status XRRSetScreenConfigAndRate(Display *dpy, XRRScreenConfiguration *config,
                                  Drawable draw, int size_index, Rotation rotation,
                                  short rate, Time timestamp)
    XRRScreenConfiguration *XRRGetScreenInfo(Display *, Window w)
    XRRScreenSize *XRRConfigSizes(XRRScreenConfiguration *config, int *nsizes)
    short *XRRConfigRates(XRRScreenConfiguration *config, int sizeID, int *nrates)
    SizeID XRRConfigCurrentConfiguration(XRRScreenConfiguration *config, Rotation *rotation)

    void XRRFreeScreenConfigInfo(XRRScreenConfiguration *)
    XRRScreenResources *XRRGetScreenResourcesCurrent(Display *dpy, Window window)
    void XRRFreeScreenResources(XRRScreenResources *resources)

    XRROutputInfo *XRRGetOutputInfo(Display *dpy, XRRScreenResources *resources, RROutput output)
    void XRRFreeOutputInfo (XRROutputInfo *outputInfo)

    XRRCrtcInfo *XRRGetCrtcInfo(Display *dpy, XRRScreenResources *resources, RRCrtc crtc)
    void XRRFreeCrtcInfo(XRRCrtcInfo *crtcInfo)

    XRRModeInfo *XRRAllocModeInfo(char *name, int nameLength)
    RRMode XRRCreateMode(Display *dpy, Window window, XRRModeInfo *modeInfo)
    void XRRDestroyMode (Display *dpy, RRMode mode)
    void XRRAddOutputMode(Display *dpy, RROutput output, RRMode mode)
    void XRRDeleteOutputMode(Display *dpy, RROutput output, RRMode mode)
    void XRRFreeModeInfo(XRRModeInfo *modeInfo)

    int XScreenCount(Display *display)
    int XDisplayWidthMM(Display *display, int screen_number)
    int XDisplayHeightMM(Display *display, int screen_number)
    int XDisplayWidth(Display *display, int screen_number)
    int XDisplayHeight(Display *display, int screen_number)

    short XRRConfigCurrentRate(XRRScreenConfiguration *config)

from xpra.x11.bindings.core_bindings cimport X11CoreBindingsInstance

cdef RandRBindingsInstance singleton = None
def RandRBindings():
    global singleton
    if singleton is None:
        singleton = RandRBindingsInstance()
    return singleton

cdef class RandRBindingsInstance(X11CoreBindingsInstance):

    cdef int _has_randr
    cdef object _added_modes

    def __init__(self):
        self._has_randr = self.check_randr() and self.check_randr_sizes()
        self._added_modes = {}

    def __repr__(self):
        return "RandRBindings(%s)" % self.display_name

    def check_randr(self):
        cdef int event_base = 0, ignored = 0, cmajor = 0, cminor = 0
        cdef int r = XRRQueryExtension(self.display, &event_base, &ignored)
        log("XRRQueryExtension()=%i", r)
        if r:
            log("found XRandR extension")
            if XRRQueryVersion(self.display, &cmajor, &cminor):
                log("found XRandR extension version %i.%i", cmajor, cminor)
                if (cmajor==1 and cminor>=2) or cmajor>=2:
                    return True
        return False

    def check_randr_sizes(self):
        #check for wayland, which has no sizes:
        #(and we wouldn't be able to set screen resolutions)
        cdef Window window = XDefaultRootWindow(self.display)
        cdef XRRScreenConfiguration *config = XRRGetScreenInfo(self.display, window)
        if config==NULL:
            log("check_randr_sizes: failed to get randr screen info")
            return False
        cdef int num_sizes = 0
        XRRConfigSizes(config, &num_sizes)
        log("found %i config sizes", num_sizes)
        return num_sizes>0

    def has_randr(self):
        return bool(self._has_randr)

    cdef _get_xrr_screen_sizes(self):
        cdef int num_sizes = 0
        cdef XRRScreenSize xrr
        cdef XRRScreenSize *xrrs = XRRSizes(self.display, 0, &num_sizes)
        sizes = []
        if xrrs==NULL:
            return sizes
        for i in range(num_sizes):
            xrr = xrrs[i]
            sizes.append((xrr.width, xrr.height))
        return sizes

    def get_xrr_screen_sizes(self):
        v = self._get_xrr_screen_sizes()
        log("get_xrr_screen_sizes()=%s", v)
        return v

    cdef _set_screen_size(self, width, height):
        self.context_check()
        cdef int num_sizes = 0
        cdef int num_rates = 0
        cdef short* rates = <short*> 0
        cdef short rate = 0
        cdef Rotation rotation = 0
        cdef Time time = 0
        cdef int sizeID = 0
        cdef XRRScreenSize *xrrs
        cdef XRRScreenSize xrr

        cdef Window window = XDefaultRootWindow(self.display)
        cdef XRRScreenConfiguration *config = XRRGetScreenInfo(self.display, window)
        if config==NULL:
            log.error("Error: failed to get randr screen info")
            return False
        try:
            xrrs = XRRConfigSizes(config, &num_sizes)
            if xrrs==NULL:
                log.error("Error: failed to get randr screen sizes")
                return False
            if num_sizes==0:
                if first_time("no-randr-sizes"):
                    log.warn("Warning: no randr sizes found")
                    log.warn(" cannot set screen size to match %ix%i", width, height)
                else:
                    log("no randr sizes")
                return False
            sizes = []
            sizeID = -1
            for i in range(num_sizes):
                xrr = xrrs[i]
                sizes.append((int(xrr.width), int(xrr.height)))
                if xrr.width==width and xrr.height==height:
                    sizeID = i
            if sizeID<0:
                log.error("Error: size not found for %ix%i" % (width, height))
                log.error(" %i sizes are supported", num_sizes)
                if num_sizes<=16:
                    log.error(" %s", csv("%ix%i" % (w,h) for w,h in sizes))
                else:
                    log("sizes found: %s", sizes)
                return False
            rates = XRRConfigRates(config, sizeID, &num_rates)
            if rates==NULL:
                log.error("Error: failed to get randr config rates")
                return False
            rate = rates[0]
            rotation = RR_Rotate_0
            time = CurrentTime
            status = XRRSetScreenConfigAndRate(self.display, config, window, sizeID, rotation, rate, time)
            if status != Success:
                log.error("Error: failed to set new screen size")
                return False
            return True
        finally:
            XRRFreeScreenConfigInfo(config)

    def get_screen_count(self):
        return XScreenCount(self.display)

    def get_screen_size_mm(self):
        sizes = self.get_screen_sizes_mm()
        tw, th = 0, 0
        for w,h in sizes:
            tw += w
            th += h
        return tw, th

    def get_screen_sizes_mm(self):
        cdef unsigned int n = XScreenCount(self.display)
        cdef unsigned int i, w, h
        cdef object sizes = []
        for i in range(n):
            w = XDisplayWidthMM(self.display, i)
            h = XDisplayHeightMM(self.display, i)
            sizes.append((w, h))
        return sizes

    def get_screen_sizes(self):
        cdef unsigned int n = XScreenCount(self.display)
        cdef unsigned int i, w, h
        cdef object sizes = []
        for i in range(n):
            w = XDisplayWidth(self.display, i)
            h = XDisplayHeight(self.display, i)
            sizes.append((w, h))
        return sizes

    def get_screen_size(self):
        return self._get_screen_size()

    def _get_screen_size(self):
        self.context_check()
        cdef XRRScreenSize *xrrs
        cdef Rotation original_rotation
        cdef int num_sizes = 0
        cdef SizeID size_id
        cdef int width, height
        cdef Window window = XDefaultRootWindow(self.display)
        cdef XRRScreenConfiguration *config = XRRGetScreenInfo(self.display, window)
        if config==NULL:
            raise Exception("failed to get screen info")
        try:
            xrrs = XRRConfigSizes(config, &num_sizes)
            if num_sizes==0:
                #on Xwayland, we get no sizes...
                #so fallback to DisplayWidth / DisplayHeight:
                return XDisplayWidth(self.display, 0), XDisplayHeight(self.display, 0)
            if xrrs==NULL:
                raise Exception("failed to get screen sizes")
            size_id = XRRConfigCurrentConfiguration(config, &original_rotation)
            if size_id<0:
                raise Exception("failed to get current configuration")
            if size_id>=num_sizes:
                raise Exception("invalid XRR size ID %i (num sizes=%i)" % (size_id, num_sizes))

            width = xrrs[size_id].width;
            height = xrrs[size_id].height;
            assert width>0 and height>0, "invalid XRR size: %ix%i" % (width, height)
            return int(width), int(height)
        finally:
            XRRFreeScreenConfigInfo(config)

    def get_vrefresh(self):
        voutputs = self.get_vrefresh_outputs()
        if voutputs:
            return min(voutputs.values())
        return self.get_vrefresh_display()

    def get_vrefresh_display(self):
        cdef Window window = XDefaultRootWindow(self.display)
        cdef XRRScreenConfiguration *config = XRRGetScreenInfo(self.display, window)
        if config==NULL:
            log.error("Error: cannot get refresh rate from screen info")
            return 0
        try:
            return XRRConfigCurrentRate(config)
        finally:
            XRRFreeScreenConfigInfo(config)

    def get_vrefresh_outputs(self):
        cdef Window window = XDefaultRootWindow(self.display)
        cdef XRROutputInfo *output_info = NULL
        cdef XRRCrtcInfo *crtc_info = NULL
        cdef XRRModeInfo *mode_info = NULL
        rates = {}
        cdef XRRScreenResources *rsc = XRRGetScreenResourcesCurrent(self.display, window)
        if rsc==NULL:
            log.error("Error: cannot access screen resources")
            return 0
        try:
            for crtc in range(rsc.ncrtc):
                crtc_info = XRRGetCrtcInfo(self.display, rsc, rsc.crtcs[crtc])
                if crtc_info==NULL:
                    log.warn("Warning: no CRTC info for %i", crtc)
                    continue
                #find the mode info:
                for i in range(rsc.nmode):
                    mode_info = &rsc.modes[i]
                    if mode_info.id==crtc_info.mode:
                        if mode_info.hTotal and mode_info.vTotal:
                            rate = iround(mode_info.dotClock / (mode_info.hTotal * mode_info.vTotal))
                            #outputs affected:
                            output_names = []
                            for o in range(crtc_info.noutput):
                                output_info = XRRGetOutputInfo(self.display, rsc, crtc_info.outputs[o])
                                if output_info!=NULL:
                                    output_names.append(decode_str(output_info.name))
                                    XRRFreeOutputInfo(output_info)
                            log("%s : %s", csv(output_names), rate)
                            rates[crtc] = rate
                        break
        finally:
            XRRFreeScreenResources(rsc)
        return rates


    def set_screen_size(self, width, height):
        return self._set_screen_size(width, height)

    def get_mode_name(self, unsigned int w, unsigned int h):
        return "%sx%s" % (w, h)

    def add_screen_size(self, unsigned int w, unsigned int h):
        self.context_check()
        log("add_screen_size(%i, %i)", w, h)
        cdef RRMode mode
        cdef RROutput output

        #monitor settings as set in xorg.conf...
        cdef unsigned int maxPixelClock = 230*1000*1000         #230MHz
        cdef unsigned int minHSync = 10*1000                    #10KHz
        cdef unsigned int maxHSync = 300*1000                   #300KHz
        cdef unsigned int minVSync = 10                         #10Hz
        cdef unsigned int maxVSync = 300                        #30Hz
        cdef double idealVSync = 50.0
        cdef double timeHFront = 0.07           #0.074219; 0.075; Width of the black border on right edge of the screen
        cdef double timeHSync = 0.1             #0.107422; 0.1125; Sync pulse duration
        cdef double timeHBack = 0.15            #0.183594; 0.1875; Width of the black border on left edge of the screen
        cdef double timeVBack = 0.06            #0.031901; 0.055664; // Adjust this to move picture up/down
        cdef double yFactor = 1                 #no interlace (0.5) or doublescan (2)

        name = self.get_mode_name(w, h)
        bname = strtobytes(name)
        cdef Window window = XDefaultRootWindow(self.display)
        cdef XRRModeInfo *new_mode = XRRAllocModeInfo(bname, len(bname))
        assert new_mode!=NULL

        cdef unsigned long clock
        try:
            xFront = int(w * timeHFront)
            xSync = int(w * timeHSync)
            xBack = int(w * timeHBack)
            xTotal = w + xFront + xSync + xBack
            yFront = 1
            ySync = 3
            yBack = int(h * timeVBack)
            yTotal = h + yFront + ySync + yBack

            modeMaxClock = maxPixelClock
            if (maxHSync * xTotal)<maxPixelClock:
                modeMaxClock = maxHSync * xTotal
            tmp = maxVSync * xTotal * yTotal * yFactor
            if tmp<modeMaxClock:
                modeMaxClock = tmp
            modeMinClock = minHSync * xTotal
            # Monitor minVSync too low? => increase mode minimum pixel clock
            tmp = minVSync * xTotal * yTotal * yFactor
            if tmp > modeMinClock:
                modeMinClock = tmp
            # If minimum clock > maximum clock, the mode is impossible...
            if modeMinClock > modeMaxClock:
                log.warn("Warning: cannot add mode %s", name)
                log.warn(" no suitable clocks could be found")
                return None

            idealClock = idealVSync * xTotal * yTotal * yFactor
            clock = idealClock;
            if clock < modeMinClock:
                clock = modeMinClock
            elif clock > modeMaxClock:
                clock = modeMaxClock

            log("Modeline %sx%s %s %s %s %s %s %s %s %s %s", w, h, clock/1000/1000,
                            w, w+xFront, w+xFront+xSync, xTotal,
                            h, h+yFront, h+yFront+ySync, yTotal)
            new_mode.width = w
            new_mode.height = h
            new_mode.dotClock = clock
            new_mode.hSyncStart = int(w+xFront)
            new_mode.hSyncEnd = int(w+xFront+xSync)
            new_mode.hTotal = int(xTotal)
            new_mode.hSkew = 0
            new_mode.vSyncStart = int(h+yFront)
            new_mode.vSyncEnd = int(h+yFront+ySync)
            new_mode.vTotal = int(yTotal)
            new_mode.modeFlags = 0
            mode = XRRCreateMode(self.display, window, new_mode)
            log("XRRCreateMode returned %#x" % mode)
            if mode<=0:
                return None
            self._added_modes[name] = int(mode)
            #now add it to the output:
            output = self.get_current_output()
            if output>0:
                log("adding mode %#x to output %#x", mode, output)
                XRRAddOutputMode(self.display, output, mode)
        finally:
            XRRFreeModeInfo(new_mode)
        if len(self._added_modes)>MAX_NEW_MODES:
            log("too many new modes (%i), trying to remove the oldest entry", len(self._added_modes))
            log("added modes=%s", csv(self._added_modes.items()))
            try:
                rname, mode = tuple(self._added_modes.items())[0]
                self.remove_mode(mode)
                del self._added_modes[rname]
            except:
                log("failed to remove older mode", exc_info=True)
        return w, h

    def remove_screen_size(self, unsigned int w, unsigned int h):
        #TODO: instead of keeping the mode ID,
        #we should query the output and find the mode dynamically...
        name = self.get_mode_name(w, h)
        cdef RRMode mode = self._added_modes.get(name, 0)
        if mode and self.remove_mode(mode):
            del self._added_modes[name]

    def remove_mode(self, RRMode mode):
        self.context_check()
        cdef RROutput output = self.get_current_output()
        log("remove_mode(%i) output=%i", mode, output)
        if mode and output:
            XRRDeleteOutputMode(self.display, output, mode)
            XRRDestroyMode(self.display, mode)

    cdef RROutput get_current_output(self):
        self.context_check()
        cdef Window window = XDefaultRootWindow(self.display)
        cdef XRRScreenResources *rsc = XRRGetScreenResourcesCurrent(self.display, window)
        assert rsc!=NULL
        try:
            log("get_current_output() screen_resources: crtcs=%s, outputs=%s, modes=%s", rsc.ncrtc, rsc.noutput, rsc.nmode)
            if rsc.noutput!=1:
                log.warn("Warning: unexpected number of outputs: %s", rsc.noutput)
                return 0
            return rsc.outputs[0]
        finally:
            XRRFreeScreenResources(rsc)

    def xrr_set_screen_size(self, w, h, xdpi, ydpi):
        self.context_check()
        #and now use it:
        cdef Window window = XDefaultRootWindow(self.display)
        wmm = iround(w*25.4/xdpi)
        hmm = iround(h*25.4/ydpi)
        log("XRRSetScreenSize(%#x, %#x, %i, %i, %i, %i)", <uintptr_t> self.display, window, w, h, wmm, hmm)
        XRRSetScreenSize(self.display, window, w, h, wmm, hmm)
