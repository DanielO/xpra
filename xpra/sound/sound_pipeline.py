# This file is part of Xpra.
# Copyright (C) 2010-2021 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

#must be done before importing gobject!
#pylint: disable=wrong-import-position

import os

from xpra.sound.gstreamer_util import import_gst, GST_FLOW_OK
gst = import_gst()
from gi.repository import GLib, GObject

from xpra.util import envint, AtomicInteger, noerr, first_time
from xpra.os_util import monotonic_time, register_SIGUSR_signals
from xpra.gtk_common.gobject_util import one_arg_signal
from xpra.log import Logger

log = Logger("sound")
gstlog = Logger("gstreamer")


KNOWN_TAGS = set((
    "bitrate", "codec", "audio-codec", "mode",
    "container-format", "encoder", "description", "language-code",
    "minimum-bitrate", "maximum-bitrate", "channel-mode",
    ))

FAULT_RATE = envint("XPRA_SOUND_FAULT_INJECTION_RATE")
SAVE_AUDIO = os.environ.get("XPRA_SAVE_AUDIO")

_counter = 0
def inject_fault():
    global FAULT_RATE
    if FAULT_RATE<=0:
        return False
    global _counter
    _counter += 1
    return (_counter % FAULT_RATE)==0


class SoundPipeline(GObject.GObject):

    generation = AtomicInteger()

    __generic_signals__ = {
        "state-changed"     : one_arg_signal,
        "error"             : one_arg_signal,
        "new-stream"        : one_arg_signal,
        "info"              : one_arg_signal,
        }

    def __init__(self, codec):
        super().__init__()
        self.stream_compressor = None
        self.codec = codec
        self.codec_description = ""
        self.codec_mode = ""
        self.container_format = ""
        self.container_description = ""
        self.bus = None
        self.bus_message_handler_id = None
        self.bitrate = -1
        self.pipeline = None
        self.pipeline_str = ""
        self.start_time = 0
        self.state = "stopped"
        self.buffer_count = 0
        self.byte_count = 0
        self.emit_info_timer = None
        self.info = {
                     "codec"        : self.codec,
                     "state"        : self.state,
                     }
        self.idle_add = GLib.idle_add
        self.timeout_add = GLib.timeout_add
        self.source_remove = GLib.source_remove
        self.file = None

    def init_file(self, codec):
        gen = self.generation.increase()
        log("init_file(%s) generation=%s, SAVE_AUDIO=%s", codec, gen, SAVE_AUDIO)
        if SAVE_AUDIO is not None:
            parts = codec.split("+")
            if len(parts)>1:
                filename = SAVE_AUDIO+str(gen)+"-"+parts[0]+".%s" % parts[1]
            else:
                filename = SAVE_AUDIO+str(gen)+".%s" % codec
            self.file = open(filename, 'wb')
            log.info("saving %s stream to %s", codec, filename)

    def save_to_file(self, *buffers):
        f = self.file
        if f and buffers:
            for x in buffers:
                self.file.write(x)
            self.file.flush()


    def idle_emit(self, sig, *args):
        self.idle_add(self.emit, sig, *args)

    def emit_info(self):
        if self.emit_info_timer:
            return
        def do_emit_info():
            self.emit_info_timer = None
            if self.pipeline:
                info = self.get_info()
                #reset info:
                self.info = {}
                self.emit("info", info)
        self.emit_info_timer = self.timeout_add(200, do_emit_info)

    def cancel_emit_info_timer(self):
        eit = self.emit_info_timer
        if eit:
            self.emit_info_timer = None
            self.source_remove(eit)


    def get_info(self) -> dict:
        info = self.info.copy()
        if inject_fault():
            info["INJECTING_NONE_FAULT"] = None
            log.warn("injecting None fault: get_info()=%s", info)
        return info

    def setup_pipeline_and_bus(self, elements):
        gstlog("pipeline elements=%s", elements)
        self.pipeline_str = " ! ".join([x for x in elements if x is not None])
        gstlog("pipeline=%s", self.pipeline_str)
        self.start_time = monotonic_time()
        try:
            self.pipeline = gst.parse_launch(self.pipeline_str)
        except Exception as e:
            self.pipeline = None
            gstlog.error("Error setting up the sound pipeline:")
            gstlog.error(" %s", e)
            gstlog.error(" GStreamer pipeline for %s:", self.codec)
            for i,x in enumerate(elements):
                gstlog.error("  %s%s", x, ["", " ! \\"][int(i<(len(elements)-1))])
            self.cleanup()
            return False
        self.bus = self.pipeline.get_bus()
        self.bus_message_handler_id = self.bus.connect("message", self.on_message)
        self.bus.add_signal_watch()
        self.info["pipeline"] = self.pipeline_str
        return True

    def do_get_state(self, state):
        if not self.pipeline:
            return  "stopped"
        return {gst.State.PLAYING   : "active",
                gst.State.PAUSED    : "paused",
                gst.State.NULL      : "stopped",
                gst.State.READY     : "ready"}.get(state, "unknown")

    def get_state(self):
        return self.state

    def update_bitrate(self, new_bitrate):
        if new_bitrate==self.bitrate:
            return
        self.bitrate = new_bitrate
        log("new bitrate: %s", self.bitrate)
        self.info["bitrate"] = new_bitrate

    def update_state(self, state):
        log("update_state(%s)", state)
        self.state = state
        self.info["state"] = state

    def inc_buffer_count(self, inc=1):
        self.buffer_count += inc
        self.info["buffer_count"] = self.buffer_count

    def inc_byte_count(self, count):
        self.byte_count += count
        self.info["bytes"]  = self.byte_count


    def set_volume(self, volume=100):
        if self.volume:
            self.volume.set_property("volume", volume/100.0)
            self.info["volume"]  = volume

    def get_volume(self):
        if self.volume:
            return int(self.volume.get_property("volume")*100)
        return GST_FLOW_OK


    def start(self):
        if not self.pipeline:
            log.error("cannot start")
            return
        register_SIGUSR_signals(self.idle_add)
        log("SoundPipeline.start() codec=%s", self.codec)
        self.idle_emit("new-stream", self.codec)
        self.update_state("active")
        self.pipeline.set_state(gst.State.PLAYING)
        if self.stream_compressor:
            self.info["stream-compressor"] = self.stream_compressor
        self.emit_info()
        #we may never get the stream start, synthesize codec event so we get logging:
        parts = self.codec.split("+")
        self.timeout_add(1000, self.new_codec_description, parts[0])
        if len(parts)>1 and parts[1]!=self.stream_compressor:
            self.timeout_add(1000, self.new_container_description, parts[1])
        elif self.container_format:
            self.timeout_add(1000, self.new_container_description, self.container_format)
        if self.stream_compressor:
            def logsc():
                self.gstloginfo("using stream compression %s", self.stream_compressor)
            self.timeout_add(1000, logsc)
        log("SoundPipeline.start() done")

    def stop(self):
        p = self.pipeline
        self.pipeline = None
        if not p:
            return
        log("SoundPipeline.stop() state=%s", self.state)
        #uncomment this to see why we end up calling stop()
        #import traceback
        #for x in traceback.format_stack():
        #    for s in x.split("\n"):
        #        v = s.replace("\r", "").replace("\n", "")
        #        if v:
        #            log(v)
        if self.state not in ("starting", "stopped", "ready", None):
            log.info("stopping")
        self.update_state("stopped")
        p.set_state(gst.State.NULL)
        log("SoundPipeline.stop() done")

    def cleanup(self):
        log("SoundPipeline.cleanup()")
        self.cancel_emit_info_timer()
        self.stop()
        b = self.bus
        self.bus = None
        log("SoundPipeline.cleanup() bus=%s", b)
        if not b:
            return
        b.remove_signal_watch()
        bmhid = self.bus_message_handler_id
        log("SoundPipeline.cleanup() bus_message_handler_id=%s", bmhid)
        if bmhid:
            self.bus_message_handler_id = None
            b.disconnect(bmhid)
        self.pipeline = None
        self.codec = None
        self.bitrate = -1
        self.state = None
        self.volume = None
        self.info = {}
        f = self.file
        if f:
            self.file = None
            noerr(f.close)
        log("SoundPipeline.cleanup() done")


    def gstloginfo(self, msg, *args):
        if self.state!="stopped":
            gstlog.info(msg, *args)
        else:
            gstlog(msg, *args)

    def gstlogwarn(self, msg, *args):
        if self.state!="stopped":
            gstlog.warn(msg, *args)
        else:
            gstlog(msg, *args)

    def new_codec_description(self, desc):
        log("new_codec_description(%s) current codec description=%s", desc, self.codec_description)
        if not desc:
            return
        dl = desc.lower()
        if dl=="wav" and self.codec_description:
            return
        cdl = self.codec_description.lower()
        if not cdl or (cdl!=dl and dl.find(cdl)<0 and cdl.find(dl)<0):
            self.gstloginfo("using '%s' audio codec", dl)
        self.codec_description = dl
        self.info["codec_description"]  = dl

    def new_container_description(self, desc):
        log("new_container_description(%s) current container description=%s", desc, self.container_description)
        if not desc:
            return
        cdl = self.container_description.lower()
        dl = {
              "mka"         : "matroska",
              "mpeg4"       : "iso fmp4",
              }.get(desc.lower(), desc.lower())
        if not cdl or (cdl!=dl and dl.find(cdl)<0 and cdl.find(dl)<0):
            self.gstloginfo("using '%s' container format", dl)
        self.container_description = dl
        self.info["container_description"]  = dl


    def on_message(self, _bus, message):
        #log("on_message(%s, %s)", bus, message)
        gstlog("on_message: %s", message)
        t = message.type
        if t == gst.MessageType.EOS:
            self.pipeline.set_state(gst.State.NULL)
            self.gstloginfo("EOS")
            self.update_state("stopped")
            self.idle_emit("state-changed", self.state)
        elif t == gst.MessageType.ERROR:
            self.pipeline.set_state(gst.State.NULL)
            err, details = message.parse_error()
            gstlog.error("Gstreamer pipeline error: %s", err.message)
            for l in err.args:
                if l!=err.message:
                    gstlog(" %s", l)
            try:
                #prettify (especially on win32):
                p = details.find("\\Source\\")
                if p>0:
                    details = details[p+len("\\Source\\"):]
                for d in details.split(": "):
                    for dl in d.splitlines():
                        if dl.strip():
                            gstlog.error(" %s", dl.strip())
            except Exception:
                gstlog.error(" %s", details)
            self.update_state("error")
            self.idle_emit("error", str(err))
            #exit
            self.cleanup()
        elif t == gst.MessageType.TAG:
            try:
                self.parse_message(message)
            except Exception as e:
                self.gstlogwarn("Warning: failed to parse gstreamer message:")
                self.gstlogwarn(" %s: %s", type(e), e)
        elif t == gst.MessageType.ELEMENT:
            try:
                self.parse_element_message(message)
            except Exception as e:
                self.gstlogwarn("Warning: failed to parse gstreamer element message:")
                self.gstlogwarn(" %s: %s", type(e), e)
        elif t == gst.MessageType.STREAM_STATUS:
            gstlog("stream status: %s", message)
        elif t == gst.MessageType.STREAM_START:
            log("stream start: %s", message)
            #with gstreamer 1.x, we don't always get the "audio-codec" message..
            #so print the codec from here instead (and assume gstreamer is using what we told it to)
            #after a delay, just in case we do get the real "audio-codec" message!
            self.timeout_add(500, self.new_codec_description, self.codec.split("+")[0])
        elif t in (gst.MessageType.ASYNC_DONE, gst.MessageType.NEW_CLOCK):
            gstlog("%s", message)
        elif t == gst.MessageType.STATE_CHANGED:
            _, new_state, _ = message.parse_state_changed()
            gstlog("state-changed on %s: %s", message.src, gst.Element.state_get_name(new_state))
            state = self.do_get_state(new_state)
            if isinstance(message.src, gst.Pipeline):
                self.update_state(state)
                self.idle_emit("state-changed", state)
        elif t == gst.MessageType.DURATION_CHANGED:
            gstlog("duration changed: %s", message)
        elif t == gst.MessageType.LATENCY:
            gstlog("latency message from %s: %s", message.src, message)
        elif t == gst.MessageType.INFO:
            self.gstloginfo("pipeline message: %s", message)
        elif t == gst.MessageType.WARNING:
            w = message.parse_warning()
            self.gstlogwarn("pipeline warning: %s", w[0].message)
            for x in w[1:]:
                for l in x.split(":"):
                    if l:
                        if l.startswith("\n"):
                            l = l.strip("\n")+" "
                            for lp in l.split(". "):
                                lp = lp.strip()
                                if lp:
                                    self.gstlogwarn(" %s", lp)
                        else:
                            self.gstlogwarn("                  %s", l.strip("\n\r"))
        else:
            self.gstlogwarn("unhandled bus message type %s: %s", t, message)
        self.emit_info()
        return GST_FLOW_OK

    def parse_element_message(self, message):
        structure = message.get_structure()
        props = {
            "seqnum"    : int(message.seqnum),
            }
        for i in range(structure.n_fields()):
            name = structure.nth_field_name(i)
            props[name] = structure.get_value(name)
        self.do_parse_element_message(message, message.src.get_name(), props)

    def do_parse_element_message(self, message, name, props=None):
        gstlog("do_parse_element_message%s", (message, name, props))

    def parse_message(self, message):
        #message parsing code for GStreamer 1.x
        taglist = message.parse_tag()
        tags = [taglist.nth_tag_name(x) for x in range(taglist.n_tags())]
        gstlog("bus message with tags=%s", tags)
        if not tags:
            #ignore it
            return
        if "bitrate" in tags:
            new_bitrate = taglist.get_uint("bitrate")
            if new_bitrate[0] is True:
                self.update_bitrate(new_bitrate[1])
                gstlog("bitrate: %s", new_bitrate[1])
        if "codec" in tags:
            desc = taglist.get_string("codec")
            if desc[0] is True:
                self.new_codec_description(desc[1])
        if "audio-codec" in tags:
            desc = taglist.get_string("audio-codec")
            if desc[0] is True:
                self.new_codec_description(desc[1])
                gstlog("audio-codec: %s", desc[1])
        if "mode" in tags:
            mode = taglist.get_string("mode")
            if mode[0] is True and self.codec_mode!=mode[1]:
                gstlog("mode: %s", mode[1])
                self.codec_mode = mode[1]
                self.info["codec_mode"] = self.codec_mode
        if "container-format" in tags:
            cf = taglist.get_string("container-format")
            if cf[0] is True:
                self.new_container_description(cf[1])
        for x in ("encoder", "description", "language-code"):
            if x in tags:
                desc = taglist.get_string(x)
                gstlog("%s: %s", x, desc[1])
        if not set(tags).intersection(KNOWN_TAGS):
            structure = message.get_structure()
            self.gstloginfo("unknown sound pipeline tag message: %s, tags=%s", structure.to_string(), tags)


    def get_element_properties(self, element, *properties):
        info = {}
        for x in properties:
            try:
                v = element.get_property(x)
                if v>=0:
                    info[x] = v
            except TypeError as e:
                if first_time("gst-property-%s" % x):
                    log("'%s' not found in %r", x, self)
                    log.warn("Warning: %s", e)
            except Exception as e:
                log.warn("Warning: %s (%s)", e, type(e))
        return info
