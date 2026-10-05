// General-purpose headless exporter; independent of any challenge.
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.util.headless.HeadlessScript;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionIterator;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;

public class Decompile extends HeadlessScript {
    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        if (args.length != 5 || analysisTimeoutOccurred()) {
            throw new IllegalStateException("Invalid arguments or incomplete analysis");
        }
        Path output = Path.of(args[0]);
        Path status = Path.of(args[1]);
        String selector = args[2];
        int seconds = Integer.parseInt(args[3]);
        int limit = Integer.parseInt(args[4]);
        byte[] footer = "\n[decompile output truncated]\n".getBytes(StandardCharsets.UTF_8);
        int remaining = limit - footer.length;
        boolean found = false;
        boolean truncated = false;
        boolean failed = false;
        DecompInterface decompiler = new DecompInterface();
        try (OutputStream stream = Files.newOutputStream(output)) {
            if (!decompiler.openProgram(currentProgram)) {
                throw new IllegalStateException("Could not open program for decompilation");
            }
            FunctionIterator functions = currentProgram.getFunctionManager().getFunctions(true);
            while (functions.hasNext()) {
                monitor.checkCancelled();
                Function function = functions.next();
                String address = function.getEntryPoint().toString();
                if (!selector.isEmpty() && !selector.equals(function.getName()) &&
                    !selector.equals(address) &&
                    !selector.equals(address.replaceFirst("^0+", ""))) {
                    continue;
                }
                found = true;
                DecompileResults result = decompiler.decompileFunction(function, seconds, monitor);
                String text = "\n/* " + function.getName() + " @ " + address + " */\n";
                if (result.decompileCompleted()) {
                    text += result.getDecompiledFunction().getC();
                }
                else {
                    failed = true;
                    text += "/* Decompilation failed: " + result.getErrorMessage() + " */\n";
                }
                byte[] bytes = text.getBytes(StandardCharsets.UTF_8);
                int count = Math.min(remaining, bytes.length);
                stream.write(bytes, 0, count);
                remaining -= count;
                if (count < bytes.length || remaining == 0) {
                    stream.write(footer);
                    truncated = true;
                    break;
                }
            }
        }
        finally {
            decompiler.dispose();
        }
        // Ghidra can exit successfully despite a script error. The wrapper also
        // requires this completion record before reporting successful output.
        Files.writeString(status, !found ? "missing" : failed ? "partial" :
            truncated ? "truncated" : "ok", StandardCharsets.UTF_8);
    }
}
