#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# This file is part of Xpra.
# Copyright (C) 2013-2019 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import sys
from threading import Lock

from xpra.codecs.loader import load_codec, get_codec, get_codec_error
from xpra.util import csv, engs
from xpra.log import Logger

log = Logger("codec", "video")

#the codec loader uses the names...
#but we need the module name to be able to probe without loading the codec:
CODEC_TO_MODULE = {
    "vpx"        : "vpx",
    "x264"       : "enc_x264",
    "x265"       : "enc_x265",
    "nvenc"      : "nvenc",
    "swscale"    : "csc_swscale",
    "cython"     : "csc_cython",
    "libyuv"     : "csc_libyuv",
    "avcodec2"   : "dec_avcodec2",
    "ffmpeg"     : "enc_ffmpeg",
    }

def has_codec_module(module_name):
    top_module = "xpra.codecs.%s" % module_name
    try:
        __import__(top_module, {}, {}, [])
        log("codec module %s is installed", module_name)
        return True
    except Exception as e:
        log("codec module %s cannot be loaded: %s", module_name, e)
        return False

def try_import_modules(*codec_names):
    names = []
    for codec_name in codec_names:
        module_name = CODEC_TO_MODULE[codec_name]
        if has_codec_module(module_name):
            names.append(codec_name)
    return names

#all the codecs we know about:
#try to import the module that contains them (cheap check):
ALL_VIDEO_ENCODER_OPTIONS = try_import_modules("x264", "vpx", "x265", "nvenc", "ffmpeg")
HARDWARE_ENCODER_OPTIONS = try_import_modules("nvenc")
ALL_CSC_MODULE_OPTIONS = try_import_modules("swscale", "cython", "libyuv")
NO_GFX_CSC_OPTIONS = []
ALL_VIDEO_DECODER_OPTIONS = try_import_modules("avcodec2", "vpx")

PREFERRED_ENCODER_ORDER = ("nvenc", "x264", "vpx", "x265")
PREFERRED_DECODER_ORDER = ("avcodec2", "vpx")
log("video_helper: ALL_VIDEO_ENCODER_OPTIONS=%s", ALL_VIDEO_ENCODER_OPTIONS)
log("video_helper: ALL_CSC_MODULE_OPTIONS=%s", ALL_CSC_MODULE_OPTIONS)
log("video_helper: NO_GFX_CSC_OPTIONS=%s", NO_GFX_CSC_OPTIONS)
log("video_helper: ALL_VIDEO_DECODER_OPTIONS=%s", ALL_VIDEO_DECODER_OPTIONS)
#for client side, using the gfx card for csc is a bit silly:
#use it for OpenGL or don't use it at all
#on top of that, there are compatibility problems with gtk at times: OpenCL AMD and TLS don't mix well


def get_encoder_module_name(x):
    if x.find("enc")>=0:
        return x                #ie: "nvenc" or "enc_vpx"
    return "enc_"+x             #ie: "enc_x264"

def get_decoder_module_name(x):
    return "dec_"+x         #ie: "dec_vpx"

def get_csc_module_name(x):
    return "csc_"+x             #ie: "csc_swscale"



def get_DEFAULT_VIDEO_ENCODERS():
    """ returns all the video encoders installed """
    encoders = []
    for x in tuple(ALL_VIDEO_ENCODER_OPTIONS):
        mod = get_encoder_module_name(x)
        c = get_codec(mod)
        if c:
            encoders.append(x)
            break
    return encoders

def get_DEFAULT_CSC_MODULES():
    """ returns all the csc modules installed """
    csc = []
    for x in tuple(ALL_CSC_MODULE_OPTIONS):
        mod = get_csc_module_name(x)
        c = get_codec(mod)
        if c:
            csc.append(x)
    return csc

def get_DEFAULT_VIDEO_DECODERS():
    """ returns all the video decoders installed """
    decoders = []
    for x in tuple(ALL_VIDEO_DECODER_OPTIONS):
        mod = get_decoder_module_name(x)
        c = get_codec(mod)
        if c:
            decoders.append(x)
    return decoders


class VideoHelper:
    """
        This class is a bit like a registry of known encoders, csc modules and decoders.
        The main instance, obtained by calling getVideoHelper, can be initialized
        by the main class, using the command line arguments.
        We can also clone it to modify it (used by per client proxy encoders)
    """

    def __init__(self, vencspecs=None, cscspecs=None, vdecspecs=None, init=False):
        self._video_encoder_specs = vencspecs or {}
        self._csc_encoder_specs = cscspecs or {}
        self._video_decoder_specs = vdecspecs or {}
        self.video_encoders = []
        self.csc_modules = []
        self.video_decoders = []

        self._cleanup_modules = []

        #bits needed to ensure we can initialize just once
        #even when called from multiple threads:
        self._initialized = init
        self._lock = Lock()

    def set_modules(self, video_encoders=(), csc_modules=(), video_decoders=()):
        log("set_modules%s", (video_encoders, csc_modules, video_decoders))
        assert not self._initialized, "too late to set modules, the helper is already initialized!"
        def filt(name, inlist, all_list):
            exclist = list(x[1:] for x in inlist if x and x.startswith("-"))
            inclist = list(x for x in inlist if x and not x.startswith("-"))
            if "all" in inclist:
                inclist = all_list
            else:
                notfound = tuple(x for x in (exclist+inclist) if x and x not in all_list)
                if notfound:
                    log.warn("Warning: ignoring unknown %s: %s", name, csv(notfound))
            return tuple(x for x in inclist if x not in exclist)
        self.video_encoders = filt("video encoders" , video_encoders,   ALL_VIDEO_ENCODER_OPTIONS)
        self.csc_modules    = filt("csc modules"    , csc_modules,      ALL_CSC_MODULE_OPTIONS)
        self.video_decoders = filt("video decoders" , video_decoders,   ALL_VIDEO_DECODER_OPTIONS)
        log("VideoHelper.set_modules(%r, %r, %r) video encoders=%s, csc=%s, video decoders=%s",
            csv(video_encoders), csv(csc_modules), csv(video_decoders),
            csv(self.video_encoders), csv(self.csc_modules), csv(self.video_decoders))

    def cleanup(self):
        with self._lock:
            #check again with lock held (in case of race):
            if not self._initialized:
                return
            cmods = self._cleanup_modules
            self._cleanup_modules = []
            log("VideoHelper.cleanup() cleanup modules=%s", cmods)
            for module in cmods:
                try:
                    module.cleanup_module()
                except Exception:
                    log.error("Error cleaning up %s", module, exc_info=True)
            self._video_encoder_specs = {}
            self._csc_encoder_specs = {}
            self._video_decoder_specs = {}
            self.video_encoders = []
            self.csc_modules = []
            self.video_decoders = []
            self._initialized = False

    def clone(self):
        if not self._initialized:
            self.init()
        #manual deep-ish copy: make new dictionaries and lists,
        #but keep the same codec specs:
        def deepish_clone_dict(indict):
            outd = {}
            for enc, d in indict.items():
                for ifmt, l in d.items():
                    for v in l:
                        outd.setdefault(enc, {}).setdefault(ifmt, []).append(v)
            return outd
        ves = deepish_clone_dict(self._video_encoder_specs)
        ces = deepish_clone_dict(self._csc_encoder_specs)
        vds = deepish_clone_dict(self._video_decoder_specs)
        return VideoHelper(ves, ces, vds, True)

    def get_info(self) -> dict:
        d = {}
        einfo = d.setdefault("encoding", {})
        dinfo = d.setdefault("decoding", {})
        cinfo = d.setdefault("csc", {})
        for encoding, encoder_specs in self._video_encoder_specs.items():
            for in_csc, specs in encoder_specs.items():
                for spec in specs:
                    einfo.setdefault("%s_to_%s" % (in_csc, encoding), []).append(spec.codec_type)
        for in_csc, out_specs in self._csc_encoder_specs.items():
            for out_csc, specs in out_specs.items():
                cinfo["%s_to_%s" % (in_csc, out_csc)] = [spec.codec_type for spec in specs]
        for encoding, decoder_specs in self._video_decoder_specs.items():
            for out_csc, decoders in decoder_specs.items():
                for decoder in decoders:
                    decoder_name = decoder[0]
                    dinfo.setdefault("%s_to_%s" % (encoding, out_csc), []).append(decoder_name)
        def modstatus(x, def_list, active_list):
            #the module is present
            if x in active_list:
                return "active"
            if x in def_list:
                return "disabled"
            return "not found"
        venc = einfo.setdefault("video-encoder", {})
        for x in ALL_VIDEO_ENCODER_OPTIONS:
            venc["%s" % x] = modstatus(x, get_DEFAULT_VIDEO_ENCODERS(), self.video_encoders)
        cscm = einfo.setdefault("csc-module", {})
        for x in ALL_CSC_MODULE_OPTIONS:
            cscm["%s" % x] = modstatus(x, get_DEFAULT_CSC_MODULES(), self.csc_modules)
        return d

    def init(self):
        log("VideoHelper.init()")
        with self._lock:
            #check again with lock held (in case of race):
            log("VideoHelper.init() initialized=%s", self._initialized)
            if self._initialized:
                return
            self.init_video_encoders_options()
            self.init_csc_options()
            self.init_video_decoders_options()
            self._initialized = True
        log("VideoHelper.init() done")

    def get_encodings(self):
        return tuple(self._video_encoder_specs.keys())

    def get_decodings(self):
        return tuple(self._video_decoder_specs.keys())

    def get_csc_inputs(self):
        return tuple(self._csc_encoder_specs.keys())


    def get_encoder_specs(self, encoding):
        return self._video_encoder_specs.get(encoding, {})

    def get_csc_specs(self, src_format):
        return self._csc_encoder_specs.get(src_format, {})

    def get_decoder_specs(self, encoding):
        return self._video_decoder_specs.get(encoding, {})


    def init_video_encoders_options(self):
        log("init_video_encoders_options()")
        log(" will try video encoders: %s", csv(self.video_encoders))
        for x in self.video_encoders:
            try:
                mod = get_encoder_module_name(x)
                load_codec(mod)
                log(" encoder for %s: %s", x, mod)
                try:
                    self.init_video_encoder_option(mod)
                except Exception as e:
                    log(" init_video_encoder_option(%s) error", mod, exc_info=True)
                    log.warn("Warning: cannot load %s video encoder:", mod)
                    log.warn(" %s", e)
                    del e
            except Exception as e:
                log("error on %s", x, exc_info=True)
                log.warn("Warning: cannot add %s encoder: %s", x, e)
                del e
        log("found %i video encoder%s: %s",
            len(self._video_encoder_specs), engs(self._video_encoder_specs), csv(self._video_encoder_specs))

    def init_video_encoder_option(self, encoder_name):
        encoder_module = get_codec(encoder_name)
        log("init_video_encoder_option(%s)", encoder_name)
        log(" module=%s", encoder_module)
        if not encoder_module:
            log(" video encoder '%s' could not be loaded:", encoder_name)
            log(" %s", get_codec_error(encoder_name))
            return
        encoder_type = encoder_module.get_type()
        encodings = encoder_module.get_encodings()
        log(" %12s encodings=%s", encoder_type, csv(encodings))
        for encoding in encodings:
            colorspaces = encoder_module.get_input_colorspaces(encoding)
            log(" %9s  input colorspaces for %5s: %s", encoder_type, encoding, csv(colorspaces))
            for colorspace in colorspaces:
                spec = encoder_module.get_spec(encoding, colorspace)
                self.add_encoder_spec(encoding, colorspace, spec)
        log("video encoder options: %s", self._video_encoder_specs)

    def add_encoder_spec(self, encoding, colorspace, spec):
        self._video_encoder_specs.setdefault(encoding, {}).setdefault(colorspace, []).append(spec)


    def init_csc_options(self):
        log("init_csc_options()")
        log(" will try csc modules: %s", csv(self.csc_modules))
        for x in self.csc_modules:
            try:
                mod = get_csc_module_name(x)
                load_codec(mod)
                self.init_csc_option(mod)
            except Exception:
                log.warn("init_csc_options() cannot add %s csc", x, exc_info=True)
        log(" csc specs: %s", csv(self._csc_encoder_specs))
        for src_format, d in sorted(self._csc_encoder_specs.items()):
            log(" %s - %s options:", src_format, len(d))
            for dst_format, specs in sorted(d.items()):
                log("  * %7s via: %s", dst_format, csv(sorted(spec.codec_type for spec in specs)))
        log("csc options: %s", self._csc_encoder_specs)

    def init_csc_option(self, csc_name):
        csc_module = get_codec(csc_name)
        log("init_csc_option(%s)", csc_name)
        log(" module=%s", csc_module)
        if csc_module is None:
            log(" csc module %s could not be loaded:", csc_name)
            log(" %s", get_codec_error(csc_name))
            return
        in_cscs = csc_module.get_input_colorspaces()
        for in_csc in in_cscs:
            out_cscs = csc_module.get_output_colorspaces(in_csc)
            log("%9s output colorspaces for %7s: %s", csc_module.get_type(), in_csc, csv(out_cscs))
            for out_csc in out_cscs:
                spec = csc_module.get_spec(in_csc, out_csc)
                self.add_csc_spec(in_csc, out_csc, spec)

    def add_csc_spec(self, in_csc, out_csc, spec):
        self._csc_encoder_specs.setdefault(in_csc, {}).setdefault(out_csc, []).append(spec)


    def init_video_decoders_options(self):
        log("init_video_decoders_options()")
        log(" will try video decoders: %s", csv(self.video_decoders))
        for x in self.video_decoders:
            try:
                mod = get_decoder_module_name(x)
                load_codec(mod)
                self.init_video_decoder_option(mod)
            except Exception:
                log.warn("Warning: cannot add %s decoder", x, exc_info=True)
        log("found %s video decoder%s: %s",
            len(self._video_decoder_specs), engs(self._video_decoder_specs), csv(self._video_decoder_specs))
        log("video decoder options: %s", self._video_decoder_specs)

    def init_video_decoder_option(self, decoder_name):
        decoder_module = get_codec(decoder_name)
        log("init_video_decoder_option(%s)", decoder_name)
        log(" module=%s", decoder_module)
        if not decoder_module:
            log(" video decoder %s could not be loaded:", decoder_name)
            log(" %s", get_codec_error(decoder_name))
            return
        decoder_type = decoder_module.get_type()
        encodings = decoder_module.get_encodings()
        log(" %s encodings=%s", decoder_type, csv(encodings))
        for encoding in encodings:
            colorspaces = decoder_module.get_input_colorspaces(encoding)
            log(" %s input colorspaces for %s: %s", decoder_type, encoding, csv(colorspaces))
            for colorspace in colorspaces:
                output_colorspace = decoder_module.get_output_colorspace(encoding, colorspace)
                log(" %s output colorspace for %5s/%7s: %s", decoder_type, encoding, colorspace, output_colorspace)
                try:
                    assert decoder_module.Decoder
                    self.add_decoder(encoding, colorspace, decoder_name, decoder_module)
                except Exception as e:
                    log.warn("failed to add decoder %s: %s", decoder_module, e)
                    del e

    def add_decoder(self, encoding, colorspace, decoder_name, decoder_module):
        self._video_decoder_specs.setdefault(
            encoding, {}).setdefault(
                colorspace, []).append(
                    (decoder_name, decoder_module)
                    )


    def get_server_full_csc_modes(self, *client_supported_csc_modes):
        """ given a list of CSC modes the client can handle,
            returns the CSC modes per encoding that the server can encode with.
            (taking into account the decoder's actual output colorspace for each encoding)
        """
        log("get_server_full_csc_modes(%s) decoder encodings=%s",
            client_supported_csc_modes, self._video_decoder_specs.keys())
        full_csc_modes = {}
        for encoding, encoding_specs in self._video_decoder_specs.items():
            assert encoding_specs is not None
            for colorspace, decoder_specs in sorted(encoding_specs.items()):
                for decoder_name, decoder_module in decoder_specs:
                    #figure out the actual output colorspace:
                    output_colorspace = decoder_module.get_output_colorspace(encoding, colorspace)
                    log("found decoder %12s for %5s with %7s mode, outputs '%s'",
                             decoder_name, encoding, colorspace, output_colorspace)
                    if output_colorspace in client_supported_csc_modes:
                        encoding_colorspaces = full_csc_modes.setdefault(encoding, [])
                        if colorspace not in encoding_colorspaces:
                            encoding_colorspaces.append(colorspace)
        log("get_server_full_csc_modes(%s)=%s", client_supported_csc_modes, full_csc_modes)
        return full_csc_modes


    def get_server_full_csc_modes_for_rgb(self, *target_rgb_modes):
        """ given a list of RGB modes the client can handle,
            returns the CSC modes per encoding that the server can encode with,
            this will include the RGB modes themselves too.
        """
        log("get_server_full_csc_modes_for_rgb%s", target_rgb_modes)
        supported_csc_modes = list(target_rgb_modes)
        for src_format, specs in self._csc_encoder_specs.items():
            for dst_format, csc_specs in specs.items():
                if dst_format in target_rgb_modes and csc_specs:
                    supported_csc_modes.append(src_format)
                    break
        supported_csc_modes = sorted(supported_csc_modes)
        return self.get_server_full_csc_modes(*supported_csc_modes)


instance = None
def getVideoHelper():
    global instance
    if instance is None:
        instance = VideoHelper()
    return instance


def main():
    from xpra.codecs.loader import log as loader_log, load_codecs
    from xpra.util import print_nested_dict
    from xpra.log import enable_color
    from xpra.platform import program_context
    with program_context("Video Helper"):
        enable_color()
        if "-v" in sys.argv or "--verbose" in sys.argv:
            loader_log.enable_debug()
            log.enable_debug()
        load_codecs()
        vh = getVideoHelper()
        vh.set_modules(ALL_VIDEO_ENCODER_OPTIONS, ALL_CSC_MODULE_OPTIONS, ALL_VIDEO_DECODER_OPTIONS)
        vh.init()
        info = vh.get_info()
        print_nested_dict(info)


if __name__ == "__main__":
    main()
