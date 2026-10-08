# Uruchomienie Red po utracie privileged intents

Cel: utrzymać tę samą instancję Red na VM po utracie uprawnień Discorda. Używamy obecnego środowiska Python i obecnych danych. Launcher może uruchomić Red z wyłączonymi privileged intents, a po powrocie pełnych uprawnień przywrócić pierwotne ustawienia.

## Zrób przed północą

1. Zapisz obecną komendę uruchomienia Red, nazwę instancji, ścieżkę do jej Pythona i ustawienia usługi, jeśli jej używasz. Zachowaj dotychczasowe argumenty Red.
2. Wdróż aktualną wersję poprawek z PR #149. Jeśli używasz Downloadera na domyślnej gałęzi, najpierw połącz sprawdzony PR z tą gałęzią. Samo otwarcie PR nie aktualizuje bota.
3. W Discordzie uruchom `[p]cog update NHCogs`, a następnie zaakceptuj proponowane przeładowanie albo wykonaj `[p]reload NHCogs`. `[p]` oznacza obecny prefix bota. Sprawdź, czy załadował się również `OperationalSupport`.
4. Skopiuj na VM plik `tools/run_red_with_intent_fallback.py` z tej samej wersji repo. Downloader instaluje pakiet `NHCogs`, więc launcher z katalogu `tools` trzeba dostarczyć osobno.
5. Zatrzymaj dotychczasowy proces Red lub jego usługę. Poczekaj, aż proces rzeczywiście się zakończy. Dopiero wtedy uruchom launcher. Nie uruchamiaj dwóch procesów z tą samą instancją i bazami.
6. Uruchom launcher poleceniem poniżej. Obserwuj konsolę i sprawdź połączenie z Discordem. Jeśli Red działa przez usługę, ustaw jej komendę startową na launcher z tym samym Pythonem i argumentami. Nie uruchamiaj równocześnie wersji ręcznej i usługi.

Nie uruchamiaj `redbot-setup` dla nowej instancji. Nie zmieniaj katalogu danych, nie resetuj konfiguracji i nie kasuj SQLite, referencji, hashy ani dowodów. Nie potrzeba reinstalacji Red lub restartu całej VM. Zmienia się komenda startowa procesu bota.

## Komendy na Windows

Podstaw rzeczywiste ścieżki i istniejącą nazwę instancji. Użyj Pythona, którym obecnie uruchamiasz Red.

```powershell
$redPython = "C:\PATH\TO\EXISTING\VENV\Scripts\python.exe"
$launcher = "C:\PATH\TO\run_red_with_intent_fallback.py"
$redInstance = "EXISTING_RED_INSTANCE"

& $redPython -m redbot --list-instances
& $redPython $launcher --help
```

Po zatrzymaniu starego procesu:

```powershell
& $redPython $launcher -- $redInstance
```

Jeżeli uprawnienia są już odebrane, zacznij od razu z wyłączonymi intents:

```powershell
& $redPython $launcher --start-degraded -- $redInstance
```

Wybierz jedno z tych dwóch poleceń startowych. Za nazwą instancji dodaj obecne argumenty Red, jeśli były używane. Jeśli Red działa jako usługa Windows, zatrzymaj właściwą usługę w jej obecnym managerze. Nie zgaduj jej nazwy i nie zatrzymuj wszystkich procesów Python.

## Komendy na Linux

```bash
red_python="/PATH/TO/EXISTING/VENV/bin/python"
red_launcher="/PATH/TO/run_red_with_intent_fallback.py"
red_instance="EXISTING_RED_INSTANCE"

"$red_python" -m redbot --list-instances
"$red_python" "$red_launcher" --help
```

Po zatrzymaniu starego procesu:

```bash
"$red_python" "$red_launcher" -- "$red_instance"
```

Jeżeli uprawnienia są już odebrane:

```bash
"$red_python" "$red_launcher" --start-degraded -- "$red_instance"
```

Wybierz jedno polecenie startowe i dodaj dotychczasowe argumenty Red za nazwą instancji. Jeśli używasz systemd, zatrzymaj znaną jednostkę i zmień jej `ExecStart`, zachowując użytkownika, środowisko i katalog roboczy usługi. Jeśli używasz innego managera procesów, zatrzymaj Red właśnie w nim. Ręczny launcher działa na pierwszym planie, więc zamknięcie terminala lub sesji SSH może go zatrzymać.

## Co zrobi launcher

Najpierw uruchamia `python -m redbot` dla wskazanej instancji z dotychczasowymi argumentami. Jeśli Red zakończy się po rozpoznanej odmowie privileged intents, launcher spróbuje uruchomić go ponownie z wyłączonymi `members`, `presences` i `message_content`. `--start-degraded` pomija pierwszą próbę z pełnym zestawem.

Wyłączone intents dotyczą całej instancji Red, także innych cogów. Red udostępnia `--disable-intent`, ale oficjalnie oznacza tę opcję jako niewspieraną. Poprawki NHCogs nie potwierdzają poprawnego działania wszystkich pozostałych cogów. Sprawdź ich komunikaty i działające komendy. [Źródło Red CLI](https://github.com/Cog-Creators/Red-DiscordBot/blob/V3/develop/redbot/core/_cli.py).

W NHCogs zadania, które potrzebują brakujących danych, pozostają w zapisanych kolejkach. Ponowne sprawdzenie następuje zwykle po 30 sekundach. Dotyczy to między innymi automatycznego wyboru reviewera i synchronizacji członków. Honeypot wstrzymuje przechwycenie brakujących załączników i nie usuwa źródła w tej ścieżce. Znane działania REST, takie jak zatwierdzony ban lub operacja z dostępnymi danymi, mogą nadal działać. Nie traktuj tego trybu jako pełnej moderacji wszystkich nowych wiadomości.

## Powrót pełnych uprawnień

Załadowany `OperationalSupport` sprawdza flagi aplikacji Discorda co 30 sekund. Gdy wrócą pełne uprawnienia dla wszystkich privileged intents pierwotnie żądanych przez launcher, zapisuje lokalny sygnał dla launchera. Samo ograniczone uprawnienie nie wystarcza do tego restartu.

Launcher zatrzymuje stary proces, czeka na jego zakończenie i uruchamia tę samą instancję z pierwotnymi argumentami. Intents, które wcześniej celowo wyłączyłeś przez `--disable-intent`, pozostają wyłączone. Po restarcie sprawdź logi, połączenie, załadowane cogi i wznowienie oczekujących zadań. Powrót może potrwać dłużej niż 30 sekund przez czas restartu lub błędy połączenia.

Automatyczny powrót wymaga uruchomienia przez launcher i działającego `OperationalSupport`. Opcja `--no-auto-restore` wyłącza automatyczny restart po odzyskaniu uprawnień. Wtedy trzeba samemu zatrzymać launcher i uruchomić Red z pierwotnymi argumentami.

Nie odzyskamy zdarzeń, których Discord nie wysłał podczas ograniczenia uprawnień lub gdy bot był offline. Nie da się też odtworzyć treści wiadomości usuniętych, zanim bot je przechwycił.

Wiadomości otrzymane podczas utraty Message Content trafiają do trwałej kolejki metadanych,
jeśli wykrywanie jest włączone. Kolejka zawiera tylko ID serwera, kanału i wiadomości oraz
czas i stan ponowień. Po odzyskaniu dostępu worker pobiera te konkretne wiadomości,
maksymalnie po 10 na przebieg. Wpisy wygasają po 14 dniach. To nie jest pobieranie
historii całych kanałów ani ponowne wykonywanie starych komend użytkowników.

## Gdy używasz gałęzi PR zamiast merge

Downloader korzysta z gałęzi ustawionej dla danego repo. `[p]cog update` nie wybiera automatycznie PR. Oficjalna składnia dodania repo z gałęzią to `[p]repo add <name> <repo_url> [branch]`. Wybór repo i przeniesienie istniejącej instalacji sprawdź przed zmianą, żeby nie instalować drugiej kopii pakietu. Najprostsza ścieżka dla istniejącego bota to merge sprawdzonego PR, potem aktualizacja obecnego `NHCogs`. [Dokumentacja Downloadera](https://docs.discord.red/en/stable/cog_guides/downloader.html#repo-add), [cog update](https://docs.discord.red/en/stable/cog_guides/downloader.html#cog-update).
