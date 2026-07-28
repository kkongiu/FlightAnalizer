# Bug List

## 1. Salvataggio note restituisce 400

`PUT /api/flights/{filename:path}/notes` riceve 400 anche con routing corretto.

**Sintomi:** La richiesta arriva al server ma risponde 400.
**Possibile causa:** Il body JSON non viene parsato correttamente. Potrebbe essere un problema del proxy Apache che non inoltra il body nelle PUT con URL contenenti spazi.
**Debug già fatto:**
- Routing corretto (route specifiche prima di `{filename:path}`)
- Aggiunta lettura raw del body con messaggi di errore espliciti
- La UI mostra ora il messaggio di errore del server
**Da fare:** Verificare il server log quando si riprova, controllare l'esatto messaggio di errore restituito.
