# certs/ — доверенные корневые сертификаты

## russian_trusted_root_ca.crt

Корневой сертификат НУЦ Минцифры («Russian Trusted Root CA»), добавляется в
системное хранилище доверия Docker-образа (см. `max_news_bot/Dockerfile`).

Причина: `platform-api2.max.ru` (Bot API мессенджера MAX) обслуживается
сертификатом, выпущенным национальным УЦ, корень которого отсутствует в
стандартном Debian-наборе `ca-certificates` и в certifi — без явного доверия
исходящий HTTPS к `*.max.ru` падает с `SSLCertVerificationError: unable to get
local issuer certificate`.

- Официальный источник: https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt
- SHA-256 fingerprint: `D2:6D:2D:02:31:B7:C3:9F:92:CC:73:85:12:BA:54:10:35:19:E4:40:5D:68:B5:BD:70:3E:97:88:CA:8E:CF:31`
- Субъект: `C=RU, O=The Ministry of Digital Development and Communications, CN=Russian Trusted Root CA`
- Годен до: 2032-02-27

Сертификат публичный, секретом не является. Проверять fingerprint при обновлении.
