import os
import ssl
import asyncio
import websockets
import mattermostdriver.websocket as mm_websocket


async def patched_connect(self, event_handler):
    # Схему берём из окружения (MM_SCHEME): https -> wss+SSL, http -> ws без SSL.
    # По умолчанию https, чтобы прод работал без изменений.
    scheme = os.getenv("MM_SCHEME", "https")

    if scheme == "https":
        context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)  # правильный клиентский контекст
        context.check_hostname = False   # не проверяем hostname сертификата
        context.verify_mode = ssl.CERT_NONE  # не проверяем сам сертификат
        ws_scheme = "wss"
        connect_kwargs = {"ssl": context}
    else:
        # локальный Mattermost по http -> WebSocket без шифрования
        ws_scheme = "ws"
        connect_kwargs = {}

    url = (
        f"{ws_scheme}://{self.options['url']}:{self.options['port']}"
        f"{self.options['basepath']}/websocket"
    )
    print("WebSocket подключается к:", url)

    self._alive = True  # флаг что бот работает
    while self._alive:  # крутимся пока бот жив
        try:
            async with websockets.connect(url, **connect_kwargs) as websocket:
                await self._authenticate_websocket(websocket, event_handler)
                await self._start_loop(websocket, event_handler)
        except Exception as e:
            print(f"Переподключение через 5с тип={type(e).__name__} ошибка={e}")
            await asyncio.sleep(5)


def sslapply():
    mm_websocket.Websocket.connect = patched_connect