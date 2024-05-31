import signal
import sys
import time
from datetime import datetime, timedelta
from dataclasses import replace
from threading import Thread
import threading

from wyzebridge import config
from wyzebridge.bridge_utils import env_bool, env_cam, is_livestream
from wyzebridge.logging import logger
from wyzebridge.mtx_server import MtxServer
from wyzebridge.stream import StreamManager
from wyzebridge.wyze_api import WyzeApi
from wyzebridge.wyze_stream import WyzeStream, WyzeStreamOptions
from wyzecam.api_models import WyzeCamera


class WyzeBridge(Thread):
    __slots__ = "api", "streams", "rtsp"

    def __init__(self) -> None:
        Thread.__init__(self)
        for sig in {"SIGTERM", "SIGINT"}:
            signal.signal(getattr(signal, sig), self.clean_up)
        print(f"\n🚀 DOCKER-WYZE-BRIDGE v{config.VERSION} {config.BUILD_STR}\n")
        self.api: WyzeApi = WyzeApi()
        self.streams: StreamManager = StreamManager()
        self.rtsp: MtxServer = MtxServer(config.BRIDGE_IP)

        if config.LLHLS:
            self.rtsp.setup_llhls(config.TOKEN_PATH, bool(config.HASS_TOKEN))

    def run(self, fresh_data: bool = False) -> None:
        self.api.login(fresh_data=fresh_data)
        # Time to run before taking a short break
        streaming_interval = timedelta(minutes=1)  # Stream for 1 minute
        # Time for a short break
        short_break_time = timedelta(seconds=15)  # Pause for 15 seconds
        # Time to run before a long break
        long_break_interval = timedelta(hours=11)  # Stream for 11 hours
        # Time for a long break
        long_break_time = timedelta(minutes=2)  # Pause for 2 minutes

        # Initialize the next break times
        next_streaming_time = datetime.now() + streaming_interval
        next_long_break_time = datetime.now() + long_break_interval

        streaming = True  # Start with streaming on
        needs_jigging = False

        # Start the streaming in a separate thread
        streaming_thread = threading.Thread(target=self.start_streaming)
        streaming_thread.start()

        logger.info("Kushal Run: Streaming has started")
        while True:
            current_time = datetime.now()

            # Check if it's time for a short break
            if streaming and current_time >= next_streaming_time:
                logger.info("Kushal Run: Taking a short break...")
                self.streams.stop_all()
                time.sleep(short_break_time.total_seconds())
                needs_jigging = False
                # After the short break, set the next streaming time
                next_streaming_time = current_time + streaming_interval + short_break_time
                streaming = False

            # Check if it's time to start streaming
            if not streaming and current_time >= next_streaming_time:
                logger.info("Kushal Run: Starting streaming...")
                streaming = True
                # Set the next streaming time after one minute
                next_streaming_time = current_time + streaming_interval

            # Check if it's time for a long break
            if current_time >= next_long_break_time:
                logger.info("Kushal Run: Taking a long break...")
                self.streams.stop_all()
                time.sleep(long_break_time.total_seconds())
                next_long_break_time = current_time + long_break_interval
                # After the long break, set the next streaming time
                next_streaming_time = current_time + streaming_interval
                streaming = False

            # Restart streaming if needed
            if not streaming_thread.is_alive() and not streaming:
                logger.info("Kushal Run: Resuming streaming...")
                streaming_thread = threading.Thread(target=self.start_streaming)
                streaming_thread.start()
                streaming = True

            time.sleep(1)  # Sleep to prevent high CPU usage

    def start_streaming(self):
        self.setup_streams()
        if self.streams.total < 1:
            signal.raise_signal(signal.SIGINT)
        self.rtsp.start()
        self.streams.monitor_streams(self.rtsp.health_check)

    def setup_streams(self):
        """Gather and setup streams for each camera."""
        logger.info("Gather and setup streams for each camera.")
        WyzeStream.user = self.api.get_user()
        WyzeStream.api = self.api
        for cam in self.api.filtered_cams():
            logger.info(f"[+] Adding {cam.nickname} [{cam.product_model}]")
            if config.SNAPSHOT_TYPE == "api":
                self.api.save_thumbnail(cam.name_uri)
            options = WyzeStreamOptions(
                quality=env_cam("quality", cam.name_uri),
                audio=bool(env_cam("enable_audio", cam.name_uri)),
                record=bool(env_cam("record", cam.name_uri)),
                reconnect=is_livestream(cam.name_uri) or not config.ON_DEMAND,
            )
            self.add_substream(cam, options)
            stream = WyzeStream(cam, options)
            stream.rtsp_fw_enabled = self.rtsp_fw_proxy(cam, stream)

            self.rtsp.add_path(stream.uri, not options.reconnect, config.WB_API)
            self.streams.add(stream)

    def rtsp_fw_proxy(self, cam: WyzeCamera, stream: WyzeStream) -> bool:
        if rtsp_fw := env_bool("rtsp_fw").lower():
            if rtsp_path := stream.check_rtsp_fw(rtsp_fw == "force"):
                rtsp_uri = f"{cam.name_uri}-fw"
                logger.info(f"Adding /{rtsp_uri} as a source")
                self.rtsp.add_source(rtsp_uri, rtsp_path)
                return True
        return False

    def add_substream(self, cam: WyzeCamera, options: WyzeStreamOptions):
        """Setup and add substream if enabled for camera."""
        if env_bool(f"SUBSTREAM_{cam.name_uri}") or (
            env_bool("SUBSTREAM") and cam.can_substream
        ):
            quality = env_cam("sub_quality", cam.name_uri, "sd30")
            record = bool(env_cam("sub_record", cam.name_uri))
            sub_opt = replace(options, substream=True, quality=quality, record=record)
            sub = WyzeStream(cam, sub_opt)
            self.rtsp.add_path(sub.uri, not options.reconnect, config.WB_API)
            self.streams.add(sub)

    def clean_up(self, *_):
        """Stop all streams and clean up before shutdown."""
        if self.streams.stop_flag:
            sys.exit(0)
        if self.streams:
            self.streams.stop_all()
        self.rtsp.stop()
        logger.info("👋 goodbye!")
        sys.exit(0)


if __name__ == "__main__":
    wb = WyzeBridge()
    wb.run()
    sys.exit(0)
