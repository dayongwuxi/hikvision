package info.seatech.hikvision;

import com.fasterxml.jackson.databind.node.ObjectNode;

public interface ArtemisApi {
    ObjectNode post(String path, ObjectNode body) throws ApiException;
}
