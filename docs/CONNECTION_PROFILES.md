# Connection profiles

## Redis and Sentinel connection profiles

The named-user tab keeps the installation's shared Redis user (default `admin`). Existing services still use this account and one shared password per browser session. A service or endpoint may instead use **Username and password**, **Password** (the Redis `default` user), or **No authentication**. Named overrides stay in the named-user catalog; password-only and open Redis services stay in the default catalog.

### One shared Redis account, open Sentinel

Keep **Global Redis user** for Redis and **No authentication** for Sentinel. Enter the shared Redis password once. This is the unchanged default for older catalogs; existing Sentinel entries are not silently given Redis credentials.

### Different users or passwords between services

Select a service, open **Connection settings**, choose **Redis** and the scope **Entire service**, then select **Username and password**. Enter the account name, optionally enter its password for this session, and click **Apply**. To override one node, select its endpoint in the scope selector instead. The **Access** button above the console opens settings for that selected endpoint.

Changing the service profile updates endpoints still inheriting the old profile; explicit endpoint overrides are kept. A specific password takes priority over the common service password. The global Redis account is used only when explicitly selected—failed passwords are not retried with another account. The panel changes connection metadata, not Redis users, ACLs or passwords.

### Sentinel authentication

Sentinel has an independent profile with **No authentication**, **Password**, and **Username and password**. Set it in **Sentinel connection** when adding a service, or open **Connection settings → Sentinel** later. Its password is never taken from Redis, even if both accounts have the same name. For a different Sentinel node password, select that endpoint's scope. This configures **Redis Farm → Sentinel**, not the Sentinel daemon's credentials for connecting to Redis.

During node scanning, optional passwords are bound to their selected Redis or Sentinel port; the two protocols do not share them. Related peers discovered within the same topology can inherit that protocol's profile. Other independent ports need their own credentials. A locked endpoint can be saved before a password is available. Changing its access settings and refreshing obtains available diagnostics; repeat discovery with access to reconcile the full service topology and service type.

For **manual ports with authentication**, explicitly choose Redis or Sentinel as the entry type. A server returning `NOAUTH` before identification cannot safely be classified by port number; the panel will ask for an explicit type rather than try both sets of credentials. Host/range mode already distinguishes the Redis and Sentinel ranges.

Under **Separate Sentinel hosts / master group**, enter one explicit `host:port` per line and optionally a master name. Those Sentinel seeds use the selected Sentinel credentials and are checked at the exact listed ports, not scanned across a new host range. Independent master groups remain separate catalog services. This release has no shared reusable Sentinel-profile object across different catalog services; credentials are kept per service/endpoint.

### Separate TLS profiles

Redis and Sentinel each have their own TCP/TLS switch, CA path, SNI name and optional client certificate/private-key paths (`--cert` / `--key`). Supply both client files for mutual TLS. These are paths on the machine running Redis Farm, not paths on the browser's computer. They do not configure the panel's HTTPS or LDAP TLS.

The application never uploads private-key contents into the catalog. TLS certificate/key files remain files provisioned by the administrator, not session passwords. Protect them with appropriate OS permissions. Certificate validation is not disabled automatically.

### Password lifetime and access states

Usernames, endpoint addresses and transport settings are shared catalog metadata. **Redis and Sentinel passwords exist only in the panel process's memory, isolated by browser session, service, protocol and endpoint.** Passwords are not written to SQLite, cookies, localStorage, connectors, exports or command-line arguments. The child CLI receives `REDISCLI_AUTH` in its private environment; CLI file history remains disabled. OS swap, core dumps and external diagnostics require separate operational controls.

**Apply** accepts connection settings and, when supplied, the session password. An empty password does not erase an existing matching secret. **Forget password** removes session credentials for that scope; it never changes a remote password. Changing a username cannot reuse another username's secret. Logout/session expiry/restart clears the session's secrets and consoles.

The interface distinguishes **Password required**, **Authentication failed**, **Command not permitted**, **Command not supported**, and connection failure. An `INFO` permission error is not reported as proof that a Redis process is down. Metadata may still be unavailable without the permissions required by that view. Raw CLI responses remain untranslated; the panel's status categories and guidance are localized.

The console remains attached to its chosen endpoint. Redis Cluster consoles accept database **0** only. Commands such as `CONNECT`, `SELECT` and CLI `-c` redirects still execute inside the CLI; they do not inherit application-side address rewriting or redefine an existing terminal's original metadata.

### Image Easter egg

Hold the left mouse button on any of the three illustrations to lift it by its leaves. The natural-sized copy follows the pointer and sways around the grip; the original returns on release, Escape or loss of focus. Normal hover does nothing. Motion respects reduced-motion preferences. This changes no Redis state.


---

## Профили подключения Redis и Sentinel

Вкладка именованного пользователя сохраняет общий Redis-профиль установки — по умолчанию `admin`. Старые сервисы продолжают использовать его и общий пароль текущей сессии. Для сервиса или конкретного инстанса можно выбрать **«Пользователь и пароль»**, **«Пароль»** (вход как `default`) либо **«Без авторизации»**. Индивидуальные именованные профили находятся в каталоге именованной вкладки; парольные и открытые Redis — во вкладке default.

### Один общий Redis-пользователь, Sentinel без пароля

Оставьте **«Общий пользователь Redis»** для Redis и **«Без авторизации»** для Sentinel. Общий пароль Redis вводится один раз. Для старых записей это поведение сохраняется: пароль Redis не начинает автоматически передаваться Sentinel.

### Разные пользователи или пароли сервисов

Выберите сервис, нажмите **«Настройки подключения»**, затем **Redis → Весь сервис → Пользователь и пароль**. Укажите имя, при необходимости пароль для текущей сессии, нажмите **«Применить»**. Для отдельного инстанса выберите его адрес в поле области действия. Кнопка **«Доступ»** над консолью открывает настройки выбранного инстанса.

Изменение общего профиля сервиса обновляет наследующие его инстансы; явно переопределённые настройки отдельных инстансов сохраняются. Пароль инстанса приоритетнее пароля сервиса. Общий Redis-профиль используется только при явном выборе; при ошибке пароля панель не перебирает другие учётные записи. Эти настройки не создают пользователей Redis, не редактируют ACL и не меняют удалённые пароли.

### Авторизация Sentinel

Для Sentinel предусмотрен отдельный профиль: **«Без авторизации»**, **«Пароль»**, **«Пользователь и пароль»**. Он настраивается в разделе **«Подключение к Sentinel»** при добавлении или через **«Настройки подключения → Sentinel»** у существующего сервиса. Пароль Redis не подставляется вместо пароля Sentinel, даже если имена пользователей совпадают. Для отдельной ноды Sentinel выберите её адрес в области действия. Это доступ **Redis Farm → Sentinel**, а не реквизиты, с которыми сам Sentinel подключается к Redis.

При сканировании необязательные пароли привязаны к выбранным портам Redis и Sentinel по отдельности. Найденные связанные участники одной топологии могут наследовать профиль своего протокола. Другим независимым портам нужны свои реквизиты. Закрытый инстанс можно сохранить до ввода пароля. После настройки доступа обновление получает доступные диагностические данные; повторное обнаружение с реквизитами уточняет полный состав и тип сервиса.

При **ручном указании защищённого порта** явно выбирайте тип Redis или Sentinel. Ответ `NOAUTH` до идентификации не позволяет надёжно определить тип процесса по одному номеру порта: панель попросит выбрать тип, а не попробует оба набора паролей. В режиме сканирования ноды диапазоны Redis и Sentinel уже разделены.

В разделе **«Отдельные хосты Sentinel / master-группа»** можно задать адреса `host:port`, по одному на строку, и необязательное имя master-группы. Эти адреса используют выбранные реквизиты Sentinel и проверяются только на указанных портах, без сканирования дополнительных диапазонов на других хостах. Независимые master-группы остаются отдельными сервисами каталога. Общего переиспользуемого объекта профиля Sentinel между разными сервисами пока нет: пароли разделены по сервисам и инстансам.

### Независимый TLS

У Redis и Sentinel отдельные переключатели TCP/TLS, CA-файл, SNI и необязательные пути к клиентскому сертификату и ключу (`--cert` / `--key`). Для взаимной TLS-аутентификации укажите оба файла. Пути относятся к машине запуска Redis Farm, не к компьютеру с браузером. Они не настраивают HTTPS панели и TLS для LDAP.

В каталог не загружается содержимое приватного ключа. Сертификат и ключ остаются файлами, которые администратор размещает отдельно; это не пароли сессии. Ограничьте доступ к ним средствами ОС. Проверка сертификатов автоматически не отключается.

### Срок жизни паролей и состояния доступа

Имена пользователей, адреса и параметры транспорта — общие метаданные каталога. **Пароли Redis и Sentinel находятся только в памяти процесса панели и разделены по браузерной сессии, сервису, протоколу и инстансу.** Они не записываются в SQLite, cookie, localStorage, connectors, экспорт или аргументы командной строки. CLI получает `REDISCLI_AUTH` в собственном окружении; файловая история CLI отключена. Swap, дампы памяти и сторонняя диагностика требуют отдельного контроля на уровне ОС.

**«Применить»** принимает параметры подключения и введённый пароль текущей сессии. Пустое поле не стирает существующий пароль того же профиля. **«Забыть пароль»** очищает реквизиты этой области в сессии, а не меняет пароль Redis. После смены имени нельзя случайно использовать секрет предыдущего пользователя. Выход, истечение сессии и перезапуск очищают её пароли и закрывают консоли.

Интерфейс различает **«Требуется пароль»**, **«Ошибка авторизации»**, **«Команда запрещена»**, **«Команда не поддерживается»** и ошибку соединения. Запрет `INFO` не доказывает, что процесс Redis выключен. Для разделов диагностики по-прежнему необходимы права на соответствующие команды. Исходные ответы CLI не переводятся; состояния и подсказки самой панели локализованы.

Открытая консоль остаётся привязана к выбранному инстансу. Для Redis Cluster доступна только DB **0**. Ручные `CONNECT`, `SELECT` и редиректы CLI `-c` выполняются внутри клиента: они не используют переписывание адресов панелью и не меняют исходные метаданные уже открытой консоли.

### Пасхалка с картинками

Удерживайте левую кнопку на любой из трёх картинок: редиска поднимается за ботву, следует за курсором в натуральном размере и покачивается вокруг точки захвата. При отпускании, Escape или потере фокуса возвращается на место. Простое наведение ничего не делает; учитываются настройки уменьшения анимации. На работу Redis эффект не влияет.

