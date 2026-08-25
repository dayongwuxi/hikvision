package info.seatech.hikvision;

public class ApiException extends Exception {
    private final boolean resultUncertain;

    public ApiException(String message, boolean resultUncertain) {
        super(message);
        this.resultUncertain = resultUncertain;
    }

    public ApiException(String message, boolean resultUncertain, Throwable cause) {
        super(message, cause);
        this.resultUncertain = resultUncertain;
    }

    public boolean isResultUncertain() {
        return resultUncertain;
    }
}
