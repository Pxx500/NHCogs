# NHCogs przy utracie dostępu do danych Discorda

Ten dokument opisuje zachowanie NHCogs, gdy funkcji brakuje wymaganych danych. Zakłada, że bot działa i pakiet jest załadowany. Nie opisuje uruchamiania Red ani przywracania jego połączenia z Discordem.

## Co sprawdzić w działającym bocie

1. Potwierdź, że działa aktualna wersja NHCogs i załadował się `OperationalSupport`.
2. Sprawdź komunikaty o dostępności `members`, `presences` i `message_content`. `OperationalSupport` odświeża flagi aplikacji co 30 sekund. Niedostępny lub nieznany wymagany stan zatrzymuje zależne zadanie.
3. Porównaj oczekujące sprawy, tickety i ostatnią poprawną synchronizację ról przed ograniczeniem dostępu i po nim. Brak danych nie powinien wyglądać jak zero członków lub zakończona sprawa.
4. Zachowaj istniejące bazy, dowody, referencje i hashe. Oczekiwanie na dane nie wymaga ich resetowania ani czyszczenia.
5. Po powrocie danych sprawdź wznowienie zadań i ich wynik. Zmiana flag nie dowodzi, że wszystkie funkcje już otrzymują potrzebne zdarzenia.

## Wiadomości i dowody Honeypot

Jeśli bot otrzyma wiadomość, ale Message Content jest niedostępny, Honeypot może zapisać ją w trwałej kolejce metadanych, gdy wykrywanie jest włączone. Kolejka zawiera ID serwera, kanału i wiadomości oraz czas i stan ponowień. Nie zawiera treści ani załączników tej wiadomości.

Po odzyskaniu dostępu worker pobiera konkretne, wcześniej zaobserwowane wiadomości. Przetwarza maksymalnie 10 wpisów na przebieg. Wpisy wygasają po 14 dniach. Nie pobiera historii całych kanałów i nie odtwarza dawnych komend użytkowników.

Jeśli istniejąca sprawa oczekuje na załączniki niedostępne przez brak Message Content, operacja zostaje odłożona. Ta ścieżka nie usuwa źródła i nie oznacza oczekujących załączników jako nieudanych tylko przez utratę dostępu. Oczekiwanie nie zużywa budżetu prób. Kolejne sprawdzenie jest zaplanowane po 30 sekundach.

Znane działania z wystarczającymi zapisanymi danymi mogą nadal działać przez REST, czyli bezpośrednie żądania do API Discorda. Dotyczy to na przykład znanego bana i operacji na roli, gdy potrzebne dane członka można pobrać. Sam brak członka w cache nie oznacza jego odejścia. Stan nieobecności wymaga potwierdzenia przez API.

## Tickety GitHub i role

Automatyczny wybór reviewera czeka, gdy nie ma wymaganych danych członków lub obecności. NHCogs odsuwa zadanie o 30 sekund. Ręczne działania, które mają wystarczające dane, nie są przez to ogólnie wyłączane.

Przed automatycznym pingiem bot ponownie sprawdza profil, kategorie, uprawnienia i członkostwo. Jeśli osoba wyłączyła automatyczne powiadomienia lub przestała spełniać warunki, niewysłana rezerwacja jest anulowana. Bot może wybrać innego reviewera bez zużycia pingu. Powiadomienie, którego wysyłanie już trwa, może się zakończyć.

Po dłuższej przerwie nowo wysłane powiadomienie otrzymuje aktualny termin odpowiedzi. Wcześniej wysłany ping jest rozpoznawany i nie powinien zostać wysłany ponownie tylko przez odzyskiwanie potwierdzenia.

Synchronizacja ról zachowuje ostatnią poprawną generację danych. Brak Members Intent lub niepełna lista członków nie powinny zastąpić jej pustym wynikiem. Zadanie pozostaje do ponowienia.

## Krótkie sprawdzenie na serwerze testowym

Sprawdzaj ograniczenia na przygotowanym bocie testowym i dobrowolnych kontach. Nie odbieraj dostępu produkcyjnemu botowi tylko po to, żeby przeprowadzić demonstrację.

- Przy braku Message Content sprawdź zapis zaobserwowanej wiadomości w kolejce metadanych bez jej treści
- Dla sprawy z oczekującym załącznikiem sprawdź zachowanie źródła i dowodów oraz termin ponowienia operacji
- Przy braku danych członków lub obecności sprawdź, czy automatyczny ticket czeka bez przypadkowego pingu
- Sprawdź, czy niepełna synchronizacja zachowuje poprzedni indeks ról
- Po powrocie danych sprawdź wznowienie zadań, aktualny termin review i brak podwójnego pingu
- Sprawdź, czy znane działanie moderacyjne z wystarczającymi danymi nadal działa przez API

## Granice tych zabezpieczeń

NHCogs nie zapewnia startu całego bota po odrzuceniu połączenia przez Discorda. Nie zmienia ustawień intents Red i nie uruchamia ponownie jego procesu. Sprawdzanie flag aplikacji nie zastępuje uzyskania uprawnień ani działającego połączenia.

Nie odzyskamy zdarzeń, których Discord nie dostarczył podczas ograniczenia dostępu lub gdy bot był offline. Nie da się odtworzyć treści wiadomości usuniętych przed przechwyceniem. Kolejka metadanych obejmuje tylko wiadomości, które bot rzeczywiście zaobserwował, i ma ograniczony czas przechowywania.

Te zabezpieczenia dotyczą NHCogs. Nie potwierdzają zachowania innych załadowanych cogów ani pełnej zgodności aplikacji z wymaganiami formularza Discorda.
