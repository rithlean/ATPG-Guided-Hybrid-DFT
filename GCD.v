module GCD_state (input clk, rst, start, input [3:0] x0, y0, output reg done, state, output reg [3:0] gcd);

reg [3:0] X, Y;
parameter S0 = 0, S1 = 1;

always @(posedge clk, posedge rst) begin

    if (rst) begin
            state <= S0;
            done <= 0;
            gcd <= 0;
    end

    else
    case(state)
        S0: begin
            done <= 0;
            if (start) begin
                state <= S1;
                X <= x0;
                Y <= y0;
                end
            else
                state <= S0;
        end
    
        S1: begin
            if (X == Y) begin
                gcd <= X;
                state <= S0;
                done <= 1;
                end
            else begin
                state <= S1;
                if (X > Y)
                    X <= X - Y;
                else
                    Y <= Y - X;
                end
            end
        endcase
    end
endmodule
