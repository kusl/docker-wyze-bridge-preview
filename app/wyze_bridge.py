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
        run_time = timedelta(minutes=1)  # Time to run
        stop_time = timedelta(seconds=15)  # Time to stop
        original_run_time = datetime.now()
        next_run_time = datetime.now() + run_time
        streaming = False
        needs_jigging = True

        # Start the streaming in a separate thread
        streaming_thread = threading.Thread(target=self.start_streaming)
        streaming_thread.start()

        logger.info("we are going to run now")
        while True:
            current_time = datetime.now()
            logger.info(f"Current time is {current_time}")

            if current_time - original_run_time >= timedelta(hours=8):
                logger.info(f"We are taking a long break here. Be back in three minutes...")
                stop_time = timedelta(seconds=180)
                original_run_time = datetime.now()
                needs_jigging = True
            elif not streaming and current_time >= next_run_time:
                if not streaming_thread.is_alive():
                    logger.info(f"We are back from our break...")
                    needs_jigging = False
                    streaming_thread = threading.Thread(target=self.start_streaming)
                    streaming_thread.start()
                streaming = True
                next_run_time = current_time + run_time
            if needs_jigging and streaming and current_time >= next_run_time:
                logger.info(f"We are taking a short break here. Be back in fifteen seconds...")
                self.streams.stop_all()
                streaming = False
                next_run_time = current_time + stop_time
            elif not streaming and current_time >= next_run_time:
                if not streaming_thread.is_alive():
                    logger.info(f"We are back from our break...")
                    needs_jigging = False
                    streaming_thread = threading.Thread(target=self.start_streaming)
                    streaming_thread.start()
                streaming = True
                next_run_time = current_time + run_time
            time.sleep(1)  # Sleep for a short time to prevent high CPU usage

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
